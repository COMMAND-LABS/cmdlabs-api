# TLDR

Documenting process of migrating Kalygo into the cmdlabs project in GCP 

##

Create a project called `command-labs`

## Store code in COMMAND LABS GitHub org

```sh
git push --set-upstream origin main --force
```

## Add `GCP_SA_KEY` as a GitHub Repository Secret

- Go to `https://console.cloud.google.com/iam-admin/iam?project=command-labs`
- Create a "Service Account"
- Download JSON associated with the "Service Account"
- Add a Repository secret called `GCP_SA` with the Service Account JSON as the value

## Enable `Artifact Registry` in GCP project

- `gcloud projects list`
- `gcloud config set project command-labs`
- `gcloud services enable artifactregistry.googleapis.com`

## Create the artifact registry for storing the built docker images

- `gcloud artifacts repositories create cmdlabs-api --repository-format docker --project command-labs --location us-central1`

## Add `Artifact Registry - Writer` permissions

``` sh
gcloud projects add-iam-policy-binding command-labs \
  --member="serviceAccount:command-labs-api-cicd@command-labs.iam.gserviceaccount.com" \
  --role="roles/artifactregistry.writer"
```

## Enable `Cloud Run` permissions

- `gcloud services enable run.googleapis.com`


## Add Cloud Run Admin perms to the CICD Service Account

```sh
gcloud projects add-iam-policy-binding command-labs \
  --member="serviceAccount:command-labs-api-cicd@command-labs.iam.gserviceaccount.com" \
  --role="roles/run.admin"
```

