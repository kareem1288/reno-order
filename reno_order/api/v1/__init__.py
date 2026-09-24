"""Version 1 of the reno_order HTTP API.

Every whitelisted endpoint of the app lives in this package, so the public
surface is in one place: /api/method/reno_order.api.v1.<module>.<function>.
Endpoints stay thin: they parse input and delegate to the service modules,
which own the business rules and permission checks.
"""
