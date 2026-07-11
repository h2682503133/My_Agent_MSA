# PV/PVC 模板

## 使用

```bash
# 在 WSL 中执行
NFS_SERVER=172.29.219.49 NFS_ROOT=/srv/nfs/my-agent bash deploy/apply-pv.sh
```

模板文件 `my-agent-nfs-pv-pvc.yaml.tpl` 使用 `${NFS_SERVER}` 和 `${NFS_ROOT}` 变量，
由 `apply-pv.sh` 渲染后 apply。
