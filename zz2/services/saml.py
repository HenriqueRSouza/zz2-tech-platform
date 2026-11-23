"""SAML 2.0 service provider for Microsoft Entra ID (Azure AD).

Lets a university account sign in through Microsoft. This module is only the
protocol: build the ``AuthnRequest``, and turn a ``SAMLResponse`` into a verified
:class:`SamlIdentity` — or refuse it. What the identity is *allowed* to do is
decided in :mod:`zz2.services.sso`.

Bindings: HTTP-Redirect for the request, HTTP-POST for the response. That is what
Entra ID's "SAML-based Sign-on" uses by default.

Validation — every check below is one a real SAML library performs, and each one
closes a known attack:

==========================================  ===================================
Check                                       Closes
==========================================  ===================================
XML signature against the configured cert   forged assertions
Use only the element the signature covers   XML signature wrapping (XSW)
Exactly one Assertion, a child of Response  XSW variants that add a 2nd one
No DTD in the document                      XXE / billion laughs
SHA-1 refused (signxml default)             signature collisions
Issuer == configured IdP entity ID          assertions from another tenant/IdP
Audience == our entity ID                   assertions minted for another app
Recipient / Destination == our ACS URL      assertions forwarded from another SP
NotBefore / NotOnOrAfter (+120 s skew)      stale or pre-dated assertions
InResponseTo == a request *we* issued,      unsolicited responses (login CSRF)
once, bound to the browser by a cookie      and replay of a captured response
Assertion ID never accepted twice           replay inside the validity window
==========================================  ===================================

Encrypted assertions are not supported; leave Entra's token encryption off.
Pending requests and the replay cache are in-memory, which is correct for this
deployment's single process (see ``deploy/gunicorn.conf.py``).
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import hmac
import secrets
import zlib
from dataclasses import dataclass, field
from urllib.parse import urlencode
from xml.sax.saxutils import escape, quoteattr

from lxml import etree
from signxml import XMLVerifier
from signxml.exceptions import SignXMLException

from zz2.core.cache import TTLCache
from zz2.core.errors import AppError
from zz2.settings import Settings, get_settings

NS = {
    "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
}
STATUS_SUCCESS = "urn:oasis:names:tc:SAML:2.0:status:Success"
BEARER = "urn:oasis:names:tc:SAML:2.0:cm:bearer"
NAMEID_EMAIL = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"

CLOCK_SKEW = dt.timedelta(seconds=120)
PENDING_TTL_SECONDS = 600.0
"""How long a sign-in may take between leaving for Microsoft and coming back."""

REPLAY_TTL_SECONDS = 3600.0
MAX_RESPONSE_BYTES = 256 * 1024

# Entra ID claim URIs.
CLAIM_EMAIL = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"
CLAIM_UPN = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name"
CLAIM_DISPLAY = "http://schemas.microsoft.com/identity/claims/displayname"
CLAIM_GIVEN = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname"
CLAIM_SURNAME = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname"
CLAIM_OID = "http://schemas.microsoft.com/identity/claims/objectidentifier"
CLAIM_TENANT = "http://schemas.microsoft.com/identity/claims/tenantid"
CLAIM_GROUPS = "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups"


class SamlError(AppError):
    """A response that failed validation.

    ``reason`` is a stable machine code for the audit log. The browser only ever
    sees the generic message: telling a forger *which* check failed is a guide to
    the next attempt.
    """

    status_code = 401
    code = "saml_rejected"

    def __init__(self, reason: str) -> None:
        super().__init__("Microsoft sign-in could not be completed.", log_detail=reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SamlIdentity:
    """What a verified assertion says about the user."""

    name_id: str
    email: str
    display_name: str
    object_id: str = ""
    tenant_id: str = ""
    groups: tuple[str, ...] = ()
    session_index: str = ""


@dataclass(frozen=True, slots=True)
class PendingLogin:
    browser_hash: str
    next_path: str


@dataclass(slots=True)
class LoginStart:
    redirect_url: str
    browser_nonce: str
    request_id: str = field(default="")


def _utc(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise SamlError("bad_timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalise_certificate(raw: str) -> str:
    """Accept the Base64 body Entra shows, or a full PEM, and return PEM."""
    body = "".join(
        line.strip() for line in raw.strip().splitlines()
        if line.strip() and "CERTIFICATE" not in line
    )
    try:
        base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("ZZ2_SAML_IDP_X509_CERT is not valid Base64") from exc
    wrapped = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN CERTIFICATE-----\n{wrapped}\n-----END CERTIFICATE-----\n"


class SamlServiceProvider:
    """Builds requests and validates responses."""

    def __init__(self) -> None:
        self._pending: TTLCache[str, PendingLogin] = TTLCache(PENDING_TTL_SECONDS, 5000)
        self._seen_assertions: TTLCache[str, bool] = TTLCache(REPLAY_TTL_SECONDS, 20_000)

    @property
    def _settings(self) -> Settings:
        return get_settings()

    def reset(self) -> None:
        """Forget pending requests and seen assertions. For tests."""
        self._pending.clear()
        self._seen_assertions.clear()

    # -- Metadata --------------------------------------------------------------
    def metadata_xml(self) -> str:
        """SP metadata, uploadable to Entra ("Upload metadata file")."""
        s = self._settings
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
            f"entityID={quoteattr(s.saml_sp_entity)}>"
            '<md:SPSSODescriptor AuthnRequestsSigned="false" WantAssertionsSigned="true" '
            'protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
            f"<md:NameIDFormat>{NAMEID_EMAIL}</md:NameIDFormat>"
            '<md:AssertionConsumerService '
            'Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
            f'Location={quoteattr(s.saml_acs_url)} index="0" isDefault="true"/>'
            "</md:SPSSODescriptor></md:EntityDescriptor>\n"
        )

    # -- Request ---------------------------------------------------------------
    def start_login(self, next_path: str) -> LoginStart:
        """Build the redirect to Microsoft and remember the request.

        Returns the redirect URL and a browser nonce. The caller puts the nonce in
        a cookie; only its hash is kept here. A response is accepted only from the
        browser holding the nonce of the request it answers — which is what stops
        an attacker completing a sign-in with *their* account in a victim's
        browser (login CSRF).
        """
        s = self._settings
        request_id = "_" + secrets.token_hex(20)
        nonce = secrets.token_urlsafe(32)
        issued = _iso(dt.datetime.now(dt.UTC))
        xml = (
            '<samlp:AuthnRequest xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
            'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
            f'ID="{request_id}" Version="2.0" IssueInstant="{issued}" '
            f"Destination={quoteattr(s.saml_idp_sso_url)} "
            f"AssertionConsumerServiceURL={quoteattr(s.saml_acs_url)} "
            'ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST">'
            f"<saml:Issuer>{escape(s.saml_sp_entity)}</saml:Issuer>"
            f'<samlp:NameIDPolicy Format="{NAMEID_EMAIL}" AllowCreate="true"/>'
            "</samlp:AuthnRequest>"
        )
        compressor = zlib.compressobj(wbits=-15)  # raw DEFLATE, per the binding
        deflated = compressor.compress(xml.encode("utf-8")) + compressor.flush()
        query = urlencode({"SAMLRequest": base64.b64encode(deflated).decode("ascii")})
        separator = "&" if "?" in s.saml_idp_sso_url else "?"

        self._pending.set(request_id, PendingLogin(_hash(nonce), next_path))
        return LoginStart(f"{s.saml_idp_sso_url}{separator}{query}", nonce, request_id)

    # -- Response --------------------------------------------------------------
    def consume(self, encoded_response: str, browser_nonce: str | None,
                *, now: dt.datetime | None = None) -> tuple[SamlIdentity, str]:
        """Validate a ``SAMLResponse`` and return ``(identity, next_path)``.

        Raises:
            SamlError: Any check failed. ``reason`` names which, for the log.
        """
        s = self._settings
        now = now or dt.datetime.now(dt.UTC)
        document = self._decode(encoded_response)

        try:
            root = etree.fromstring(document, parser=_safe_parser())
        except etree.XMLSyntaxError as exc:
            raise SamlError("malformed_xml") from exc
        if root.tag != f"{{{NS['samlp']}}}Response":
            raise SamlError("not_a_response")

        # Status first: a failed sign-in at Microsoft (user cancelled, not
        # assigned to the app) arrives unsigned, and deserves a clear log line.
        status = root.find("samlp:Status/samlp:StatusCode", NS)
        if status is None or status.get("Value") != STATUS_SUCCESS:
            raise SamlError("idp_status_" + (status.get("Value", "missing").rsplit(":", 1)[-1]
                                              if status is not None else "missing"))

        if len(root.findall(".//saml:Assertion", NS)) != 1:
            raise SamlError("assertion_count")
        if root.findall(".//saml:EncryptedAssertion", NS):
            raise SamlError("encrypted_assertion_unsupported")

        assertion = self._verified_assertion(document, root)

        destination = root.get("Destination")
        if destination is not None and destination != s.saml_acs_url:
            raise SamlError("destination_mismatch")

        issuer = assertion.findtext("saml:Issuer", namespaces=NS)
        if (issuer or "").strip() != s.saml_idp_entity_id:
            raise SamlError("issuer_mismatch")

        self._check_conditions(assertion, now)
        in_response_to = self._check_subject_confirmation(assertion, now)

        # Consume the pending request exactly once, before any other outcome:
        # a response whose request was consumed is a replay whatever else it is.
        pending = self._pending.get(in_response_to)
        self._pending.invalidate(in_response_to)
        if pending is None:
            raise SamlError("unknown_or_reused_request")
        if not browser_nonce or not hmac.compare_digest(pending.browser_hash,
                                                        _hash(browser_nonce)):
            raise SamlError("browser_binding_mismatch")

        assertion_id = assertion.get("ID") or ""
        if not assertion_id or self._seen_assertions.get(assertion_id):
            raise SamlError("assertion_replayed")
        self._seen_assertions.set(assertion_id, True)

        return self._identity(assertion), pending.next_path

    # -- Steps -----------------------------------------------------------------
    @staticmethod
    def _decode(encoded: str) -> bytes:
        if not encoded or len(encoded) > MAX_RESPONSE_BYTES * 4 // 3 + 4:
            raise SamlError("response_size")
        try:
            document = base64.b64decode(encoded, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise SamlError("bad_base64") from exc
        if len(document) > MAX_RESPONSE_BYTES:
            raise SamlError("response_size")
        if b"<!DOCTYPE" in document.upper() or b"<!ENTITY" in document.upper():
            raise SamlError("dtd_forbidden")
        return document

    def _verified_assertion(self, document: bytes, root: etree._Element) -> etree._Element:
        """Verify the signature and return the assertion *it covers*.

        Everything after this point reads from the returned element, never from
        ``root``. Reading from the parsed document instead is exactly the bug XML
        signature wrapping exploits: the signature is valid over one element while
        the code trusts a different, injected one.
        """
        cert = normalise_certificate(self._settings.saml_idp_x509_cert.get_secret_value())
        try:
            result = XMLVerifier().verify(document, x509_cert=cert,
                                          parser=_safe_parser())
        except SignXMLException as exc:
            raise SamlError("signature_invalid") from exc
        if isinstance(result, list):
            raise SamlError("signature_ambiguous")

        signed = result.signed_xml
        if signed is None:
            raise SamlError("signature_invalid")
        if signed.tag == f"{{{NS['samlp']}}}Response":
            assertions = signed.findall("saml:Assertion", NS)
            if len(assertions) != 1:
                raise SamlError("assertion_count")
            return assertions[0]
        if signed.tag == f"{{{NS['saml']}}}Assertion":
            # The signed assertion must be the one sitting directly under the
            # Response, not a copy tucked somewhere else in the tree.
            direct = root.findall("saml:Assertion", NS)
            if len(direct) != 1 or direct[0].get("ID") != signed.get("ID"):
                raise SamlError("assertion_position")
            return signed
        raise SamlError("signature_wrong_element")

    def _check_conditions(self, assertion: etree._Element, now: dt.datetime) -> None:
        conditions = assertion.find("saml:Conditions", NS)
        if conditions is None:
            raise SamlError("conditions_missing")
        not_before = _utc(conditions.get("NotBefore"))
        not_after = _utc(conditions.get("NotOnOrAfter"))
        if not_before is not None and now + CLOCK_SKEW < not_before:
            raise SamlError("not_yet_valid")
        if not_after is None or now - CLOCK_SKEW >= not_after:
            raise SamlError("expired")
        audiences = [a.text.strip() for a in conditions.findall(
            "saml:AudienceRestriction/saml:Audience", NS) if a.text]
        if self._settings.saml_sp_entity not in audiences:
            raise SamlError("audience_mismatch")

    def _check_subject_confirmation(self, assertion: etree._Element,
                                    now: dt.datetime) -> str:
        for confirmation in assertion.findall("saml:Subject/saml:SubjectConfirmation", NS):
            if confirmation.get("Method") != BEARER:
                continue
            data = confirmation.find("saml:SubjectConfirmationData", NS)
            if data is None:
                continue
            if data.get("Recipient") != self._settings.saml_acs_url:
                raise SamlError("recipient_mismatch")
            not_after = _utc(data.get("NotOnOrAfter"))
            if not_after is None or now - CLOCK_SKEW >= not_after:
                raise SamlError("confirmation_expired")
            in_response_to = data.get("InResponseTo") or ""
            if not in_response_to:
                # IdP-initiated sign-in. Refused: an unsolicited response can be
                # delivered to any browser. Point the Entra "Sign on URL" at
                # /api/auth/saml/login and the My Apps tile becomes SP-initiated.
                raise SamlError("unsolicited_response")
            return in_response_to
        raise SamlError("bearer_confirmation_missing")

    @staticmethod
    def _identity(assertion: etree._Element) -> SamlIdentity:
        attributes: dict[str, list[str]] = {}
        for attribute in assertion.findall("saml:AttributeStatement/saml:Attribute", NS):
            name = attribute.get("Name") or ""
            values = [(v.text or "").strip()
                      for v in attribute.findall("saml:AttributeValue", NS)]
            attributes.setdefault(name, []).extend(v for v in values if v)

        def first(*names: str) -> str:
            for name in names:
                if attributes.get(name):
                    return attributes[name][0]
            return ""

        name_id = (assertion.findtext("saml:Subject/saml:NameID", namespaces=NS) or "").strip()
        email = first(CLAIM_EMAIL, CLAIM_UPN) or (name_id if "@" in name_id else "")
        display = first(CLAIM_DISPLAY) or " ".join(
            part for part in (first(CLAIM_GIVEN), first(CLAIM_SURNAME)) if part
        ) or email
        authn = assertion.find("saml:AuthnStatement", NS)
        return SamlIdentity(
            name_id=name_id,
            email=email.lower(),
            display_name=display,
            object_id=first(CLAIM_OID),
            tenant_id=first(CLAIM_TENANT),
            groups=tuple(attributes.get(CLAIM_GROUPS, [])),
            session_index=(authn.get("SessionIndex") or "") if authn is not None else "",
        )


def _safe_parser() -> etree.XMLParser:
    """No entities, no DTDs, no network — the XXE surface switched off."""
    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                           huge_tree=False, remove_comments=False)


saml_sp = SamlServiceProvider()
