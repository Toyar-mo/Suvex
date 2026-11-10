---
name: kubectl
description: kubectl usage inside the sandbox for Kubernetes misconfiguration testing.
---

# kubectl

Installed as `/usr/local/bin/kubectl` (official release binary).

```bash
kubectl config view
kubectl get ns,pods,deploy,svc -A -o wide
kubectl auth can-i --list
kubectl get secrets -A
kubectl exec -it <pod> -n <ns> -- sh
```

Rules:
- If there is no kubeconfig, say so; do not fabricate cluster access.
- `auth can-i --list` answers "what can this identity do" before probing.
- Reading `Secrets` is credential access: record the namespace/object, never
  paste decoded secrets into the report.
