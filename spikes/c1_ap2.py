"""Spike C1: create an AP2 mandate with MandateClient.create, verify it from a clean install.

Run in a clean venv (Python 3.11+):
    uv venv --python 3.12 /tmp/c1venv
    uv pip install --python /tmp/c1venv/bin/python "git+https://github.com/google-agentic-commerce/AP2"
    /tmp/c1venv/bin/python spikes/c1_ap2.py
Not part of the engine; it needs the AP2 SDK, which is not on PyPI.
"""

import json

from ap2.sdk.generated.open_payment_mandate import AllowedPayees, AmountRange, OpenPaymentMandate
from ap2.sdk.generated.types.merchant import Merchant
from ap2.sdk.mandate import MandateClient
from cryptography.hazmat.primitives.asymmetric import ec
from jwcrypto.jwk import JWK
from jwcrypto.jws import InvalidJWSSignature

issuer = ec.generate_private_key(ec.SECP256R1())
holder = ec.generate_private_key(ec.SECP256R1())
payload = OpenPaymentMandate(
    constraints=[
        AmountRange(currency="USD", max=8000),
        AllowedPayees(allowed=[Merchant(id="demo-shoe-store", name="Demo Shoe Store")]),
    ],
    cnf={"jwk": json.loads(JWK.from_pyca(holder.public_key()).export_public())},
)
token = MandateClient().create(payloads=[payload], issuer_key=JWK.from_pyca(issuer))
print("token chars:", len(token))
out = MandateClient().verify(token, JWK.from_pyca(issuer.public_key()), payload_type=OpenPaymentMandate)
print("verified:", type(out).__name__, out.mandate_payload.constraints[0].max)
try:
    MandateClient().verify(
        token, JWK.from_pyca(holder.public_key()), payload_type=OpenPaymentMandate
    )
    print("WRONG KEY ACCEPTED")
except InvalidJWSSignature as e:
    print("wrong key rejected:", type(e).__name__)
