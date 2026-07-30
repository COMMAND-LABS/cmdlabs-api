# TLDR

Documentation related to Strip

##

https://docs.stripe.com/connect/testing

##

docker run --rm -it --network agent-network stripe/stripe-cli \
  listen --api-key "$(grep '^STRIPE_SECRET_KEY=' cmdlabs-api/.env | cut -d= -f2)" \
  --forward-to http://cmdlabs-api:4000/api/billing/webhook

  ##

  stripe webhook_endpoints create \
  --api-key "<STRIPE_SK_KEY_HERE>" \
  --url https://api.cmdlabs.io/api/billing/webhook \
  --api-version 2024-06-20 \
  --description "cmdlabs-api production billing" \
  --enabled-events checkout.session.completed \
  --enabled-events customer.subscription.created \
  --enabled-events customer.subscription.updated \
  --enabled-events customer.subscription.deleted

  ##

  gcloud secrets add-iam-policy-binding STRIPE_MEMBER_PRICE_ID \
  --member="serviceAccount:382688591561-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

  ##

  gcloud secrets add-iam-policy-binding STRIPE_WEBHOOK_SECRET \
  --member="serviceAccount:382688591561-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"