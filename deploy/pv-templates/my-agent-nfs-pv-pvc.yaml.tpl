apiVersion: v1
kind: Namespace
metadata:
  name: agent
---
# ============================================================
# My_Agent MSA NFS PV/PVC
#
# 由 apply-nfs-pv.sh 通过 envsubst/sed 渲染生成。
#
# 注意：
#   storageClassName: ""
#   是必须的，用来避免默认 StorageClass，比如 standard，
#   干扰手写静态 NFS PV/PVC 的绑定。
#
# 变量：
#   ${NFS_SERVER} -> NFS server IP 或域名
#   ${NFS_ROOT}   -> NFS export 根路径，例如 /srv/nfs/my-agent
# ============================================================

apiVersion: v1
kind: PersistentVolume
metadata:
  name: my-agent-config-pv
spec:
  storageClassName: ""
  capacity:
    storage: 2Gi
  accessModes:
  - ReadWriteMany
  persistentVolumeReclaimPolicy: Retain
  nfs:
    server: ${NFS_SERVER}
    path: ${NFS_ROOT}/config
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: my-agent-config-pvc
  namespace: agent
spec:
  storageClassName: ""
  accessModes:
  - ReadWriteMany
  volumeName: my-agent-config-pv
  resources:
    requests:
      storage: 2Gi
---
apiVersion: v1
kind: PersistentVolume
metadata:
  name: my-agent-openviking-pv
spec:
  storageClassName: ""
  capacity:
    storage: 5Gi
  accessModes:
  - ReadWriteMany
  persistentVolumeReclaimPolicy: Retain
  nfs:
    server: ${NFS_SERVER}
    path: ${NFS_ROOT}/openviking/viking_data
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: my-agent-openviking-pvc
  namespace: agent
spec:
  storageClassName: ""
  accessModes:
  - ReadWriteMany
  volumeName: my-agent-openviking-pv
  resources:
    requests:
      storage: 5Gi
---
apiVersion: v1
kind: PersistentVolume
metadata:
  name: my-agent-assets-pv
spec:
  storageClassName: ""
  capacity:
    storage: 10Gi
  accessModes:
  - ReadWriteMany
  persistentVolumeReclaimPolicy: Retain
  nfs:
    server: ${NFS_SERVER}
    path: ${NFS_ROOT}/assets
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: my-agent-assets-pvc
  namespace: agent
spec:
  storageClassName: ""
  accessModes:
  - ReadWriteMany
  volumeName: my-agent-assets-pv
  resources:
    requests:
      storage: 10Gi
---
apiVersion: v1
kind: PersistentVolume
metadata:
  name: my-agent-workspace-pv
spec:
  storageClassName: ""
  capacity:
    storage: 20Gi
  accessModes:
  - ReadWriteMany
  persistentVolumeReclaimPolicy: Retain
  nfs:
    server: ${NFS_SERVER}
    path: ${NFS_ROOT}/workspace
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: my-agent-workspace-pvc
  namespace: agent
spec:
  storageClassName: ""
  accessModes:
  - ReadWriteMany
  volumeName: my-agent-workspace-pv
  resources:
    requests:
      storage: 20Gi
---
apiVersion: v1
kind: PersistentVolume
metadata:
  name: my-agent-timer-tasks-pv
spec:
  storageClassName: ""
  capacity:
    storage: 1Gi
  accessModes:
  - ReadWriteMany
  persistentVolumeReclaimPolicy: Retain
  nfs:
    server: ${NFS_SERVER}
    path: ${NFS_ROOT}/timer-tasks
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: my-agent-timer-tasks-pvc
  namespace: agent
spec:
  storageClassName: ""
  accessModes:
  - ReadWriteMany
  volumeName: my-agent-timer-tasks-pv
  resources:
    requests:
      storage: 1Gi

---
apiVersion: v1
kind: PersistentVolume
metadata:
  name: my-agent-user-data-pv
spec:
  storageClassName: ""
  capacity:
    storage: 1Gi
  accessModes:
  - ReadWriteMany
  persistentVolumeReclaimPolicy: Retain
  nfs:
    server: ${NFS_SERVER}
    path: ${NFS_ROOT}/user-data
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: my-agent-user-data-pvc
  namespace: agent
spec:
  storageClassName: ""
  accessModes:
  - ReadWriteMany
  volumeName: my-agent-user-data-pv
  resources:
    requests:
      storage: 1Gi
