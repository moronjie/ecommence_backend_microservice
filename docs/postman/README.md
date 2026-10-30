# Postman collection

`shop.postman_collection.json` has every endpoint the gateway exposes (52 requests), grouped by service.

1. Postman: **Import** > pick `shop.postman_collection.json`.
2. Open the collection's **Variables** tab. `baseUrl` is `http://localhost:8000` (the gateway).
   Set `adminPassword` to the `ADMIN_PASSWORD` your stack was started with.
3. Run the **Start here** folder: register, log in (tokens are saved for you), log in as admin.
4. Use any other request. Ids (product, order, address, payment reference) are saved as you go.

Create a product as the admin first (log in as admin, then *Create Product*), then log in as the customer
again before using the cart and orders.

Regenerate it after the API changes (the stack must be running):

    make postman

You can also try everything in the browser at http://localhost:8000/docs (Authorize button for your token).
It is on by default and off when `ENVIRONMENT=production`; set `DOCS_ENABLED=true` to override.
