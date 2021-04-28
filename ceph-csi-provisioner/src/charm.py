#!/usr/bin/env python3

import json
import kubernetes
import logging
import os
import socket

from pathlib import Path
from ops.charm import CharmBase
from ops.main import main
from ops.framework import StoredState
from ops.model import ActiveStatus, MaintenanceStatus
from oci_image import OCIImageResource, OCIImageResourceError

log = logging.getLogger(__name__)


class CephCsiCharm(CharmBase):
    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)

        self.framework.observe(self.on.install, self.set_pod_spec)
        self.framework.observe(self.on.upgrade_charm, self.set_pod_spec)
        self.framework.observe(self.on.config_changed, self.set_pod_spec)
        self.framework.observe(self.on["ceph"].relation_changed, self.set_pod_spec)

        self.csi_image = OCIImageResource(self, "csi-image")
        self.provisioner_image = OCIImageResource(self, "provisioner-image")
        self.resizer_image = OCIImageResource(self, "resizer-image")
        self.snapshotter_image = OCIImageResource(self, "snapshotter-image")
        self.attacher_image = OCIImageResource(self, "attacher-image")

        self.driver_name = "cephfs.csi.ceph.com"

    @staticmethod
    def _get_kubernetes_environment():
        """
        Workaround due to this bug: https://bugs.launchpad.net/juju/+bug/1892255
        """
        cluster_env = {}
        for e in Path("/proc/1/environ").read_text().split("\x00"):
            if "KUBERNETES_SERVICE" in e:
                key, value = e.split("=")
                cluster_env[key] = value

        os.environ.update(cluster_env)

    def apply_storage_class(self):
        """
        Currently, v3 Juju pod spec does not support StorageClass.  We'll
        have to deploy it directly via the Kubernetes API.
        """
        self._get_kubernetes_environment()

        configuration = kubernetes.config.load_incluster_config()
        with kubernetes.client.ApiClient(configuration) as api_client:
            api_instance = kubernetes.client.StorageV1beta1Api(api_client)
            sc = {
                "provisioner": self.driver_name,
                "reclaim_policy": self.model.config.get("reclaim-policy"),
                "allow_volume_expansion": self.model.config.get(
                    "allow-volume-expansion"
                ),
                "mount_options": self.model.config.get("mount-options").split(","),
                "parameters": {
                    "clusterID": self.model.config.get("cluster-id"),
                    "fsName": self.model.config.get("fs-name"),
                    "pool": self.model.config.get("pool-name"),
                    "imageFeatures": "layering",
                    "csi.storage.k8s.io/provisioner-secret-name": "ceph-csi-secret",
                    "csi.storage.k8s.io/provisioner-secret-namespace": self.model.name,
                    "csi.storage.k8s.io/controller-expand-secret-name": "ceph-csi-secret",
                    "csi.storage.k8s.io/controller-expand-secret-namespace": self.model.name,
                    "csi.storage.k8s.io/node-stage-secret-name": "ceph-csi-secret",
                    "csi.storage.k8s.io/node-stage-secret-namespace": self.model.name,
                    "csi.storage.k8s.io/fstype": self.model.config.get("fs-type"),
                },
                "metadata": {"name": "ceph-csi-sc"},
            }
            try:
                api_instance.create_storage_class(sc)
            except:
                self.model.unit.status = MaintenanceStatus(
                    "Failed to apply StorageClass"
                )
                self.remove_storage_class()
                self.apply_storage_class()

    def remove_storage_class(self):
        """
        Due to StorageClass not being namespaces and `create_storage_class` erroring
        when trying to re-apply, we need to clean up the existing StorageClass on certain
        events.
        """
        self._get_kubernetes_environment()

        configuration = kubernetes.config.load_incluster_config()
        with kubernetes.client.ApiClient(configuration) as api_client:
            api_instance = kubernetes.client.StorageV1beta1Api(api_client)
            api_instance.delete_storage_class("ceph-csi-sc")

    def set_pod_spec(self, event):
        """
        Setup all the compononets needed.
        """
        if event in [
            self.on.remove,
            self.on.config_changed,
            self.on["ceph"].relation_changed,
        ]:
            self.delete_storage_class()

        ceph_user = None
        ceph_key = None
        ceph_monitors = []
        if self.model.relations.get("ceph"):
            ceph = self.model.relations["ceph"]
            for relation in ceph:
                for unit in list(relation.units):
                    ceph_user = relation.data[unit]["auth"]
                    ceph_key = relation.data[unit]["key"]
                    ceph_monitors.append(relation.data[unit]["ceph-public-address"])

        if not ceph_user or not ceph_key:
            self.model.unit.status = MaintenanceStatus("Waiting on ceph relation")
            event.defer()

        try:
            csi_image = self.csi_image.fetch()
            provisioner_image = self.provisioner_image.fetch()
            resizer_image = self.resizer_image.fetch()
            snapshotter_image = self.snapshotter_image.fetch()
            attacher_image = self.attacher_image.fetch()
        except OCIImageResourceError as e:
            self.model.unit.status = e.status
            log.error(e)
            return

        csi_config = [
            {
                "clusterID": self.model.config.get("cluster-id"),
                "monitors": ceph_monitors,
            }
        ]

        csi_socket = {
            "container": "/csi/csi.sock",
            "host": "/var/lib/kubelet/plugins{}/csi.sock".format(self.driver_name),
        }

        csi_volume = {
            "name": "socket-dir",
            "mountPath": os.path.dirname(str(csi_socket.get("container"))),
            "hostPath": {
                "path": os.path.dirname(str(csi_socket.get("host"))),
                "type": "DirectoryOrCreate",
            },
        }

        default_environment = {
            "NODE_ID": {"field": {"path": "spec.nodeName", "api-version": "v1"}},
            "POD_IP": {"field": {"path": "status.podIP", "api-version": "v1"}},
            "CSI_ENDPOINT": "unix://{}".format(csi_socket.get("container")),
        }

        self.model.unit.status = MaintenanceStatus("Setting pod spec")
        self.model.pod.set_spec(
            {
                "version": 3,
                "containers": [
                    {
                        "name": "ceph-provisioner",
                        "imageDetails": provisioner_image,
                        "args": [
                            "--csi-address={}".format(csi_socket.get("container")),
                            "--v=5",
                            "--timeout=150s",
                            "--leader-election=true",
                            "--retry-interval-start=500ms",
                            "--feature-gates=Topology=false",
                            "--extra-create-metadata=true",
                        ],
                        "ports": [
                            {
                                "name": "metrics",
                                "containerPort": int(self.model.config["metrics-port"]),
                            }
                        ],
                        "volumeConfig": [csi_volume],
                    },
                    {
                        "name": "ceph-resizer",
                        "imageDetails": resizer_image,
                        "args": [
                            "--csi-address={}".format(csi_socket.get("container")),
                            "--v=5",
                            "--timeout=150s",
                            "--leader-election=true",
                            "--retry-interval-start=500ms",
                            "--handle-volume-inuse-error=false",
                        ],
                        "volumeConfig": [csi_volume],
                        "envConfig": default_environment,
                    },
                    {
                        "name": "ceph-snapshotter",
                        "imageDetails": snapshotter_image,
                        "args": [
                            "--csi-address={}".format(csi_socket.get("container")),
                            "--v=5",
                            "--timeout=150s",
                            "--leader-election=true",
                        ],
                        "volumeConfig": [csi_volume],
                        "kubernetes": {
                            "securityContext": {
                                "privileged": True,
                            }
                        },
                    },
                    {
                        "name": "csi-cephfsplugin-attacher",
                        "imageDetails": attacher_image,
                        "args": [
                            "--csi-address={}".format(csi_socket.get("container")),
                            "--v=5",
                            "--leader-election=true",
                            "--retry-interval-start=500ms",
                        ],
                        "volumeConfig": [csi_volume],
                        "envConfig": default_environment,
                    },
                    {
                        "name": "csi-cephfsplugin",
                        "imageDetails": csi_image,
                        "args": [
                            "--nodeid={}".format(socket.gethostname()),
                            "--type=cephfs",
                            "--controllerserver=true",
                            "--endpoint=unix://{}".format(csi_socket.get("container")),
                            "--v=5",
                            "--drivername={}".format(self.driver_name),
                            "--pidlimit=-1",
                        ],
                        "volumeConfig": [
                            csi_volume,
                            {
                                "name": "mountpoint-dir",
                                "mountPath": "/var/lib/kubelet/pods",
                                "hostPath": {
                                    "path": "/var/lib/kubelet/pods",
                                    "type": "DirectoryOrCreate",
                                },
                            },
                            {
                                "name": "plugin-dir",
                                "mountPath": "/var/lib/kubelet/plugins",
                                "hostPath": {
                                    "path": "/var/lib/kubelet/plugins",
                                    "type": "Directory",
                                },
                            },
                            {
                                "name": "host-sys",
                                "mountPath": "/sys",
                                "hostPath": {"path": "/sys"},
                            },
                            {
                                "name": "lib-modules",
                                "mountPath": "/lib/modules",
                                "hostPath": {"path": "/lib/modules"},
                            },
                            {
                                "name": "host-dev",
                                "mountPath": "/dev",
                                "hostPath": {"path": "/dev"},
                            },
                            {
                                "name": "host-mount",
                                "mountPath": "/run/mount",
                                "hostPath": {"path": "/run/mount"},
                            },
                            {
                                "name": "keys-tmp-dir",
                                "mountPath": "/tmp/csi/keys",
                                "hostPath": {"path": "/tmp/csi/keys"},
                            },
                            {
                                "name": "ceph-csi-config",
                                "mountPath": "/etc/ceph-csi-config",
                                "hostPath": {"path": "/etc/ceph-csi-config"},
                            },
                        ],
                        "envConfig": default_environment,
                        "kubernetes": {
                            "securityContext": {
                                "privileged": True,
                            }
                        },
                    },
                    {
                        "name": "liveness-prometheus",
                        "imageDetails": csi_image,
                        "args": [
                            "--type=liveness",
                            "--endpoint=unix://{}".format(csi_socket.get("container")),
                            "--metricsport={}".format(
                                self.model.config.get("metrics-port")
                            ),
                            "--metricspath=/metrics",
                            "--polltime=60s",
                            "--timeout=3s",
                        ],
                        "volumeConfig": [
                            {
                                "name": "socket-dir",
                                "mountPath": os.path.dirname(
                                    str(csi_socket.get("container"))
                                ),
                                "hostPath": {
                                    "path": os.path.dirname(
                                        str(csi_socket.get("host"))
                                    ),
                                    "type": "DirectoryOrCreate",
                                },
                            }
                        ],
                        "envConfig": default_environment,
                    },
                ],
            },
            k8s_resources={
                "kubernetesResources": {
                    "serviceAccounts": [
                        {
                            "name": "cephfs-csi-provisioner",
                            "roles": [
                                {
                                    "name": "cephfs-csi-provisioner-runner",
                                    "global": True,
                                    "rules": [
                                        {
                                            "apiGroups": [""],
                                            "resources": ["nodes"],
                                            "verbs": ["get", "list", "watch"],
                                        },
                                        {
                                            "apiGroups": [""],
                                            "resources": ["secrets"],
                                            "verbs": ["get", "list"],
                                        },
                                        {
                                            "apiGroups": [""],
                                            "resources": ["events"],
                                            "verbs": [
                                                "list",
                                                "watch",
                                                "create",
                                                "update",
                                                "patch",
                                            ],
                                        },
                                        {
                                            "apiGroups": [""],
                                            "resources": ["persistentvolumes"],
                                            "verbs": [
                                                "get",
                                                "list",
                                                "watch",
                                                "create",
                                                "delete",
                                                "patch",
                                            ],
                                        },
                                        {
                                            "apiGroups": [""],
                                            "resources": ["persistentvolumeclaims"],
                                            "verbs": ["get", "list", "watch", "update"],
                                        },
                                        {
                                            "apiGroups": ["storage.k8s.io"],
                                            "resources": ["storageclasses"],
                                            "verbs": ["get", "list", "watch"],
                                        },
                                        {
                                            "apiGroups": ["snapshot.storage.k8s.io"],
                                            "resources": ["volumesnapshots"],
                                            "verbs": ["get", "list"],
                                        },
                                        {
                                            "apiGroups": ["snapshot.storage.k8s.io"],
                                            "resources": ["volumesnapshotcontents"],
                                            "verbs": [
                                                "create",
                                                "get",
                                                "list",
                                                "watch",
                                                "update",
                                                "delete",
                                            ],
                                        },
                                        {
                                            "apiGroups": ["snapshot.storage.k8s.io"],
                                            "resources": ["volumesnapshotclasses"],
                                            "verbs": ["get", "list", "watch"],
                                        },
                                        {
                                            "apiGroups": ["storage.k8s.io"],
                                            "resources": ["volumeattachments"],
                                            "verbs": [
                                                "get",
                                                "list",
                                                "watch",
                                                "update",
                                                "patch",
                                            ],
                                        },
                                        {
                                            "apiGroups": ["storage.k8s.io"],
                                            "resources": ["volumeattachments/status"],
                                            "verbs": ["patch"],
                                        },
                                        {
                                            "apiGroups": [""],
                                            "resources": [
                                                "persistentvolumeclaims/status"
                                            ],
                                            "verbs": ["update", "patch"],
                                        },
                                        {
                                            "apiGroups": ["storage.k8s.io"],
                                            "resources": ["csinodes"],
                                            "verbs": ["get", "list", "watch"],
                                        },
                                        {
                                            "apiGroups": ["snapshot.storage.k8s.io"],
                                            "resources": [
                                                "volumesnapshotcontents/status"
                                            ],
                                            "verbs": ["update"],
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                    "secrets": [
                        {
                            "name": "ceph-csi-secret",
                            "stringData": {
                                "adminID": ceph_user,
                                "adminKey": ceph_key,
                            },
                        }
                    ],
                },
                "configMaps": {
                    "ceph-csi-config": {"config.json": json.dumps(csi_config)}
                },
            },
        )
        self.apply_storage_class()
        self.model.unit.status = ActiveStatus()


if __name__ == "__main__":
    main(CephCsiCharm)