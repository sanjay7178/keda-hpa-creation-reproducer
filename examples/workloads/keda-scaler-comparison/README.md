# KEDA HPA creation timing workload

This is the workload used by the standalone reproducer. Download the release
binary using the root [README](../../../README.md).

From the repository root:

```bash
export KUBE_BURNER_BIN=/absolute/path/to/kube-burner
export KUBECONFIG=/path/to/dedicated-cluster-kubeconfig
export ALLOW_KEDA_RESET=true
./run.sh smoke
./run.sh compare
```

The runner reinstalls KEDA and deletes its CRDs between releases. Use a
disposable cluster. Generated results remain local and are ignored by Git.
