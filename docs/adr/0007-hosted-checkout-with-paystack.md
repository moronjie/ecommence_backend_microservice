# 0007. Paystack's hosted page takes the payment; we never see card details

## Context
Shoppers in Ghana pay mostly with mobile money (MTN MoMo, Vodafone Cash, AirtelTigo) and cards. Handling card numbers
ourselves would bring heavy security obligations (PCI-DSS), and building each mobile money flow would be a project of
its own.

## Decision
Use **Paystack's hosted checkout**. Our payment service starts a transaction and gets a link; the customer pays on
Paystack's own page, with whatever method they prefer. Card and wallet details never reach our servers. Paystack tells us
the result by **webhook**, and we do not take its word for it: the webhook is accepted only with a valid HMAC signature, and
the service then **asks Paystack to confirm** the status, amount and currency before announcing `payment.completed`. A
reconcile job re-checks payments whose webhook never arrived, and unfinished payments expire after 25 minutes.

Everything provider-specific sits behind a small interface (`PaymentProvider`), with a fake implementation for development.
The fake provider and its "pay now" endpoint can never run in production (the service refuses to start).

## Consequences
* **Good:** a far smaller security burden; mobile money works from day one; the provider can be replaced by writing one
  class.
* **Cost:** the customer leaves our site to pay, so the frontend must redirect to the link and handle the return.
* **Cost:** results arrive asynchronously, so an order spends time in `awaiting_payment`, and a payment can arrive after
  the order was cancelled. That case is handled: the order service asks for a refund.
* **Cost:** the Paystack client was written from the public API and has only been exercised against a stand-in. It must be
  tried with a Paystack **test** key (and its refund webhook payload confirmed) before real money is involved.
