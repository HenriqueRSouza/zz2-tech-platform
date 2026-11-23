"""A stand-in for Microsoft Entra ID that signs SAML responses like the real one.

Assertion signed with RSA-SHA256 and exclusive c14n, the Entra default. Tests then
tamper with the signed output to prove each check rejects what it should.
"""

from __future__ import annotations

import base64
import datetime as dt
import uuid
import zlib
from urllib.parse import parse_qs, urlparse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from lxml import etree
from signxml import XMLSigner

IDP_ENTITY = "https://sts.windows.net/00000000-0000-0000-0000-000000000001/"
IDP_SSO_URL = "https://login.microsoftonline.com/00000000-0000-0000-0000-000000000001/saml2"
SP_PUBLIC_URL = "https://suporte.example.edu"
SP_ENTITY = f"{SP_PUBLIC_URL}/api/auth/saml/metadata"
ACS_URL = f"{SP_PUBLIC_URL}/api/auth/saml/acs"
EXC_C14N = "http://www.w3.org/2001/10/xml-exc-c14n#"


def make_keypair(common_name: str = "Microsoft Azure Federated SSO Certificate"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = dt.datetime.now(dt.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=365))
            .sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    return key_pem, cert_pem


def cert_body(cert_pem: bytes) -> str:
    """The Base64 body only — what Entra's "Certificate (Base64)" download holds."""
    return "".join(line for line in cert_pem.decode().splitlines() if "CERTIFICATE" not in line)


def request_id_from_redirect(location: str) -> str:
    encoded = parse_qs(urlparse(location).query)["SAMLRequest"][0]
    xml = zlib.decompress(base64.b64decode(encoded), -15)
    return etree.fromstring(xml).get("ID")


def _iso(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def build_response(
    *,
    key_pem: bytes,
    cert_pem: bytes,
    in_response_to: str | None,
    email: str = "aluno@mackenzista.com.br",
    display: str = "Aluno Teste",
    groups: tuple[str, ...] = (),
    audience: str = SP_ENTITY,
    issuer: str = IDP_ENTITY,
    recipient: str = ACS_URL,
    now: dt.datetime | None = None,
    lifetime: dt.timedelta = dt.timedelta(minutes=5),
    status: str = "urn:oasis:names:tc:SAML:2.0:status:Success",
    sign: bool = True,
    sign_response: bool = False,
) -> bytes:
    now = now or dt.datetime.now(dt.UTC)
    assertion_id = "_" + uuid.uuid4().hex
    irt = f' InResponseTo="{in_response_to}"' if in_response_to else ""
    group_values = "".join(f"<AttributeValue>{g}</AttributeValue>" for g in groups)
    group_attr = (
        '<Attribute Name="http://schemas.microsoft.com/ws/2008/06/identity/claims/groups">'
        f"{group_values}</Attribute>" if groups else ""
    )
    assertion = f"""<Assertion xmlns="urn:oasis:names:tc:SAML:2.0:assertion" ID="{assertion_id}" IssueInstant="{_iso(now)}" Version="2.0"><Issuer>{issuer}</Issuer><Subject><NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">{email}</NameID><SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer"><SubjectConfirmationData{irt} NotOnOrAfter="{_iso(now + lifetime)}" Recipient="{recipient}"/></SubjectConfirmation></Subject><Conditions NotBefore="{_iso(now - dt.timedelta(minutes=1))}" NotOnOrAfter="{_iso(now + lifetime)}"><AudienceRestriction><Audience>{audience}</Audience></AudienceRestriction></Conditions><AttributeStatement><Attribute Name="http://schemas.microsoft.com/identity/claims/objectidentifier"><AttributeValue>11111111-2222-3333-4444-555555555555</AttributeValue></Attribute><Attribute Name="http://schemas.microsoft.com/identity/claims/tenantid"><AttributeValue>00000000-0000-0000-0000-000000000001</AttributeValue></Attribute><Attribute Name="http://schemas.microsoft.com/identity/claims/displayname"><AttributeValue>{display}</AttributeValue></Attribute><Attribute Name="http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"><AttributeValue>{email}</AttributeValue></Attribute>{group_attr}</AttributeStatement><AuthnStatement AuthnInstant="{_iso(now)}" SessionIndex="{assertion_id}"><AuthnContext><AuthnContextClassRef>urn:oasis:names:tc:SAML:2.0:ac:classes:Password</AuthnContextClassRef></AuthnContext></AuthnStatement></Assertion>"""  # noqa: E501

    element = etree.fromstring(assertion.encode())
    if sign:
        element = XMLSigner(c14n_algorithm=EXC_C14N).sign(
            element, key=key_pem, cert=cert_pem.decode(), reference_uri=assertion_id,
        )
    signed_assertion = etree.tostring(element).decode()
    response_id = "_" + uuid.uuid4().hex
    response = (
        '<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
        f'ID="{response_id}" Version="2.0" IssueInstant="{_iso(now)}" '
        f'Destination="{recipient}"{irt}>'
        f'<Issuer xmlns="urn:oasis:names:tc:SAML:2.0:assertion">{issuer}</Issuer>'
        f'<samlp:Status><samlp:StatusCode Value="{status}"/></samlp:Status>'
        f"{signed_assertion}</samlp:Response>"
    )
    if sign_response:
        signed = XMLSigner(c14n_algorithm=EXC_C14N).sign(
            etree.fromstring(response.encode()), key=key_pem, cert=cert_pem.decode(),
            reference_uri=response_id,
        )
        return etree.tostring(signed)
    return response.encode()


def encode(document: bytes) -> str:
    return base64.b64encode(document).decode()
