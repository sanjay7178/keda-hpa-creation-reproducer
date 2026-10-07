# KEDA HPA creation timing workload

This is the workload used by the standalone reproducer. Install the pinned
kube-burner build and follow the root [README](../../../README.md).

From the repository root:

```bash
./scripts/install-kube-burner.sh
export KUBECONFIG=/path/to/dedicated-cluster-kubeconfig
export ALLOW_KEDA_RESET=true
./run.sh smoke
./run.sh compare
```

The runner reinstalls KEDA and deletes its CRDs between releases. Use a
disposable cluster. Generated results remain local and are ignored by Git.
