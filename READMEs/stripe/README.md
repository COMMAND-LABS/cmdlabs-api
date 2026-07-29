# TLDR

Documentation related to Strip

##

https://docs.stripe.com/connect/testing

##

docker run --rm -it --network agent-network stripe/stripe-cli \
  listen --api-key "$(grep '^STRIPE_SECRET_KEY=' cmdlabs-api/.env | cut -d= -f2)" \
  --forward-to http://cmdlabs-api:4000/api/billing/webhook