# TLDR

No, it's related to how the background process for knowledge ingestion works.

Long story short, it's implemented as Google Cloud Functions.

https://console.cloud.google.com/run?deploymentType=function&project=kalygo-436411


## GCP Permissions for Knowledge Base Ingest Service Account

- Storage Admin (GCS)
- Pub/Sub Editor (GCP Pub/Sub)