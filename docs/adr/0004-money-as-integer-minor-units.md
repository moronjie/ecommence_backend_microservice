# 0004. Money is stored as whole minor units, never decimals

## Context
Prices and totals must add up exactly. Floating-point numbers cannot represent most decimal amounts (0.1 + 0.2 is not
0.3), and decimal types are rounded differently by different databases, languages and libraries.

## Decision
Every amount is an **integer count of the currency's smallest unit**: `price_cents`, `total_cents`, `amount_cents`.
For Ghana that is pesewas, so `24000` means GHS 240.00. Amounts are formatted only at the edge (emails, the frontend).
The API rejects `19.99` where money is expected. Every amount travels with its currency code, and a cart holds one
currency. Paystack also counts in minor units, so no conversion happens on the way to the provider.

(The fields are called `*_cents` for historical reasons; they hold pesewas when the currency is GHS.)

## Consequences
* **Good:** sums, comparisons and totals are exact everywhere; the order total can be checked against its items;
  the payment amount sent to Paystack and the amount confirmed back are compared as plain integers.
* **Cost:** every client must divide by 100 to display a price, and must know the currency's minor unit (a currency
  with no minor unit, or three decimals, would need a small mapping; only GHS is accepted today).
* **Cost:** the `*_cents` name is slightly misleading for non-cent currencies.
