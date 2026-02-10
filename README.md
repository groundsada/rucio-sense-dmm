# Data Movement Manager (DMM)

Data Movement Manager (DMM) for the Rucio-SENSE interoperation prototype.
DMM is the interface between Rucio (/FTS) and SENSE, making SDN operated HEP data-flows possible

## Quickstart
### Running in Kubernetes (Recommended)
1. Create Configuration Secrets (see etc/mksecrets.sh)
2. Create Deployment
```
kubectl apply -f etc/deploy.yaml
```