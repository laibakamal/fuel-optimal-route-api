"""Pure-Python domain services. No Django imports anywhere in this package.

Keeping the routing/corridor/optimiser logic free of Django means the part of
this codebase that actually decides where to buy fuel can be unit-tested against
hand-computed fixtures without a database, a settings module, or a test client.
"""
