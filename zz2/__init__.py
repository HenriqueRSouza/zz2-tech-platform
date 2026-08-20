"""ZZ2 Tech Support Platform.

A modular replacement for the single-file ``server.py`` monolith. Layers, and the
rule that keeps them honest — each depends only on the ones below it:

``api``
    HTTP: routing, request/response schemas, middleware, authorization
    dependencies. Knows about FastAPI; contains no business rules.

``services``
    Business logic and orchestration. Knows nothing about HTTP.

``repositories``
    Persistence. The only layer that touches storage.

``gateways``
    Third-party integrations (Supabase, Google Apps Script).

``domain``
    Entities and pure rules. Depends on nothing.

``core``
    Cross-cutting primitives: logging, errors, atomic storage, caching, events.

``workers``
    Background loops. Composes services; owned by the application lifespan.
"""

__version__ = "2.0.0"
