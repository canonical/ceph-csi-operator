#!/usr/bin/env python3

import logging
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

        provisioner_socket = "unix:///csi/csi-provisioner.sock"
        host_provisioner_socket = "/var/lib/kubelet/plugins/cephfs.csi.ceph.com/"

        self.model.unit.status = MaintenanceStatus("Setting pod spec")
        self.model.pod.set_spec(
            {
                "version": 3,
                "containers": [
                    {
                        "name": "ceph-csi",
                        "imageDetails": csi_image,
                        "args": [
                            f"--nodeid={socket.gethostname()}",
                            "--type=cephfs",
                            "--nodeserver=true",
                            f"--endpoint={provisioner_socket}",
                            "--v=5",
                            "--drivername=cephfs.csi.ceph.com",
                            "--pidlimit=-1",
                        ],
                        "envConfig": {"CSI_ENDPOINT": provisioner_socket},
                        "volumeConfig": [
                            {
                                "name": "socket-dir",
                                "mountPath": "/csi",
                                "hostPath": {
                                    "path": host_provisioner_socket,
                                    "type": "DirectoryOrCreate",
                                },
                            }
                        ],
                        "ports": [
                            {
                                "name": "metrics",
                                "containerPort": int(self.model.config["metrics-port"]),
                            }
                        ],
                    },
                    {
                        "name": "ceph-provisioner",
                        "imageDetails": provisioner_image,
                        "args": [
                            f"--csi-address={provisioner_socket}",
                            "--v=5",
                            "--timeout=150s",
                            "--leader-election=true",
                            "--retry-interval-start=500ms",
                            "--feature-gates=Topology=false",
                            "--extra-create-metadata=true",
                        ],
                        "envConfig": {"ADDRESS": provisioner_socket},
                        "volumeConfig": [
                            {
                                "name": "socket-dir",
                                "mountPath": "/csi",
                                "hostPath": {
                                    "path": host_provisioner_socket,
                                    "type": "DirectoryOrCreate",
                                },
                            }
                        ],
                    },
                    {
                        "name": "ceph-resizer",
                        "imageDetails": resizer_image,
                        "args": [
                            f"--csi-address={provisioner_socket}",
                            "--v=5",
                            "--timeout=150s",
                            "--leader-election=true",
                            "--retry-interval-start=500ms",
                            "--handle-volume-inuse-error=false",
                        ],
                        "envConfig": {"ADDRESS": provisioner_socket},
                        "volumeConfig": [
                            {
                                "name": "socket-dir",
                                "mountPath": "/csi",
                                "hostPath": {
                                    "path": host_provisioner_socket,
                                    "type": "DirectoryOrCreate",
                                },
                            }
                        ],
                    },
                    {
                        "name": "ceph-snapshotter",
                        "imageDetails": snapshotter_image,
                        "args": [
                            f"--csi-address={provisioner_socket}",
                            "--v=5",
                            "--timeout=150s",
                            "--leader-election=true",
                        ],
                        "envConfig": {"ADDRESS": provisioner_socket},
                        "volumeConfig": [
                            {
                                "name": "socket-dir",
                                "mountPath": "/csi",
                                "hostPath": {
                                    "path": host_provisioner_socket,
                                    "type": "DirectoryOrCreate",
                                },
                            }
                        ],
                    },
                    {
                        "name": "ceph-attacher",
                        "imageDetails": attacher_image,
                        "args": [
                            f"--csi-address={provisioner_socket}",
                            "--v=5",
                            "--leader-election=true",
                            "--retry-interval-start=500ms",
                        ],
                        "envConfig": {"ADDRESS": provisioner_socket},
                        "volumeConfig": [
                            {
                                "name": "socket-dir",
                                "mountPath": "/csi",
                                "hostPath": {
                                    "path": host_provisioner_socket,
                                    "type": "DirectoryOrCreate",
                                },
                            }
                        ],
                    },
                ],
            }
        )
        self.model.unit.status = ActiveStatus()


if __name__ == "__main__":
    main(CephCsiCharm)
