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
- 