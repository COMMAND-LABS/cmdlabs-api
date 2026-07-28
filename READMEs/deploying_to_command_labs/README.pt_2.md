# TLDR

Documenting process of migrating Kalygo into the cmdlabs project in GCP 

## Setting up DNS (ie: point api.cmdlabs.io -> the Cloud Run service for the `cmdlabs-api`)

`gcloud domains verify cmdlabs.io`

```sh
gcloud beta run domain-mappings create \
  --service=cmdlabs-api-service \
  --domain=api.cmdlabs.io \
  --region=us-east1 \
  --project=command-labs
```

## Add the appropriate record to the DNS config

- `api   CNAME        ghs.googlehosted.com.`

## For testing the status of the cert 

```
gcloud beta run domain-mappings describe \
  --domain=api.cmdlabs.io --region=us-east1 --project=command-labs \
  --format="yaml(status.conditions)"
```

