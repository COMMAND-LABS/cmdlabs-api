##

gcloud auth login

## 

gcloud iam service-accounts create cmdlabs-runner-sa \
  --project $PROJECT --display-name "cmdlabs runner (no roles by design)"

##

PROJECT=command-labs
REGION=us-east1

##

gcloud iam service-accounts create cmdlabs-runner-sa \
  --project $PROJECT --display-name "cmdlabs runner (no roles by design)"

VERIFY: https://console.cloud.google.com/iam-admin/serviceaccounts

## 

gcloud auth configure-docker us-central1-docker.pkg.dev

##

cd runner

##

docker build --platform linux/amd64 -t us-central1-docker.pkg.dev/$PROJECT/cmdlabs-api/cmdlabs-runner:latest . 

##

docker push us-central1-docker.pkg.dev/$PROJECT/cmdlabs-api/cmdlabs-runner:latest

##

gcloud run services replace service.yaml --project $PROJECT --region $REGION

### TROUBLESHOOTING

```sh
docker buildx imagetools inspect us-central1-docker.pkg.dev/$PROJECT/cmdlabs-api/cmdlabs-runner:latest
docker buildx build --platform linux/amd64 --provenance=false --sbom=false \
  -t us-central1-docker.pkg.dev/$PROJECT/cmdlabs-api/cmdlabs-runner:latest --push .
docker buildx imagetools inspect us-central1-docker.pkg.dev/$PROJECT/cmdlabs-api/cmdlabs-runner:latest
gcloud run services replace service.yaml --project $PROJECT --region $REGION
gcloud services enable compute.googleapis.com
gcloud run services replace service.yaml --project $PROJECT --region $REGION # WORKED √ - I think it was the compute.googleapis.com that needed to be enabled
```

##

gcloud run services add-iam-policy-binding cmdlabs-runner --project $PROJECT --region $REGION \
  --member "serviceAccount:$PROJECT_NUMBER-compute@developer.gserviceaccount.com" --role roles/run.invoker

##

TOKEN=$(gcloud auth print-identity-token) 

##

