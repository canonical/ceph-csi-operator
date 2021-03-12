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
        self.registrar_image = OCIImageResource(self, "registrar-image")

        self.framework.observe(self.on.install, self.set_pod_spec)
        self.framework.observe(self.on.upgrade_charm, self.set_pod_spec)
        self.framework.observe(self.on.config_changed, self.set_pod_spec)

    def set_pod_spec(self, event):
        try:
            csi_image = self.csi_image.fetch()
            registrar_image = self.registrar_image.fetch()
        except OCIImageResourceError as e:
            self.model.unit.status = e.status
            log.error(e)
            return

        csi_socket = "unix:///csi/csi.sock"
        host_socket_directory = "/var/lib/kubelet/plugins/cephfs.csi.ceph.com/"

        self.model.unit.status = MaintenanceStatus("Setting pod spec")
        self.model.pod.set_spec(
            {
                "version": 3,
                "containers": [
                    {
                        "name": "ceph-registrar",
                        "imageDetails": registrar_image,
                        "args": [
                            "--v=5",
                            f"--csi-address={csi_socket}",
                            f"--kubelet-registration-path={host_socket_directory}csi.sock",
                        ],
                        "ports": [
                            {
                                "name": "metrics",
                                "containerPort": int(self.model.config["metrics-port"]),
                            }
                        ],
                    },
                    {
                        "name": "ceph-csi",
                        "imageDetails": csi_image,
                        "args": [
                            f"--nodeid={socket.gethostname()}",
                            "--type=cephfs",
                            "--nodeserver=true",
                            f"--endpoint={csi_socket}",
                            "--v=5",
                            "--drivername=cephfs.csi.ceph.com",
                        ],
                        "volumeConfig": [
                            {
                                "name": "socket-dir",
                                "mountPath": "/csi",
                                "hostPath": {
                                    "path": host_socket_directory,
                                    "type": "DirectoryOrCreate",
                                },
                            }
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
