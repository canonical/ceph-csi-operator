#!/usr/bin/env python3

import logging
import os
import socket

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

        self.csi_image = OCIImageResource(self, "csi-image")
        self.provisioner_image = OCIImageResource(self, "provisioner-image")
        self.resizer_image = OCIImageResource(self, "resizer-image")
        self.snapshotter_image = OCIImageResource(self, "snapshotter-image")
        self.attacher_image = OCIImageResource(self, "attacher-image")

        self.framework.observe(self.on.install, self.set_pod_spec)
        self.framework.observe(self.on.upgrade_charm, self.set_pod_spec)
        self.framework.observe(self.on.config_changed, self.set_pod_spec)

    def set_pod_spec(self, event):
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

        driver_name = "cephfs.csi.ceph.com"

        csi_socket = {
            "container": "/csi/csi.sock",
            "host": "/var/lib/kubelet/plugins{}/csi.sock".format(driver_name),
        }

        csi_volume = {
            "name": "socket-dir",
            "mountPath": os.path.dirname(csi_socket.get("container")),
            "hostPath": {
                "path": os.path.dirname(csi_socket.get("host")),
                "type": "DirectoryOrCreate",
            },
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
                    },
                    {
                        "name": "ceph-attacher",
                        "imageDetails": attacher_image,
                        "args": [
                            "--csi-address={}".format(csi_socket.get("container")),
                            "--v=5",
                            "--leader-election=true",
                            "--retry-interval-start=500ms",
                        ],
                        "volumeConfig": [csi_volume],
                    },
                    {
                        "name": "ceph-csi",
                        "imageDetails": csi_image,
                        "args": [
                            "--nodeid={}".format(socket.gethostname()),
                            "--type=cephfs",
                            "--nodeserver=true",
                            "--endpoint=unix://{}".format(csi_socket.get("container")),
                            "--v=5",
                            "--drivername={}".format(driver_name),
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
                        ],
                        "kubernetes": {
                            "securityContext": {
                                "privileged": True,
                            }
                        },
                    },
                ],
            }
        )
        self.model.unit.status = ActiveStatus()


if __name__ == "__main__":
    main(CephCsiCharm)
