# infra/terraform

GCP infrastructure: Cloud Run, Cloud SQL + pgvector, GCS, Cloud Tasks, Secret Manager, and KMS. This is
currently a skeleton with the provider and variables only, and no resources. Owner: infra agent.

```
terraform init -backend=false && terraform validate
```
