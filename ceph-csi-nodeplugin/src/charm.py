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

        driver_name = "cephfs.csi.ceph.com"

        csi_socket = {
            "container": "/csi/csi.sock",
            "host": "/var/lib/kubelet/plugins{}/csi.sock".format(driver_name),
        }
        registration_socket = {
            "container": "/registration/{}-reg.sock".format(driver_name),
            "host": "/var/lib/kubelet/plugins_registry/{}-reg.sock".format(driver_name),
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
                        "name": "ceph-registrar",
                        "imageDetails": registrar_image,
                        "args": [
                            "--v=5",
                            "--csi-address={}".format(csi_socket.get("container")),
                            "--kubelet-registration-path={}".format(
                                registration_socket.get("host")
                            ),
                        ],
                        "ports": [
                            {
                                "name": "metrics",
                                "containerPort": int(self.model.config["metrics-port"]),
                            }
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
                            },
                            {
                                "name": "registration-dir",
                                "mountPath": os.path.dirname(
                                    str(registration_socket.get("container"))
                                ),
                                "hostPath": {
                                    "path": os.path.dirname(
                                        str(registration_socket.get("host"))
                                    ),
                                    "type": "DirectoryOrCreate",
                                },
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
                        "name": "csi-cephfsplugin",
                        "imageDetails": csi_image,
                        "args": [
                            "--nodeid={}".format(socket.gethostname()),
                            "--type=cephfs",
                            "--nodeserver=true",
                            "--endpoint=unix://{}".format(csi_socket.get("container")),
                            "--v=5",
                            "--drivername={}".format(driver_name),
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
                            },
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
                        "envConfig": {
                            "NODE_ID": {
                                "field": {"path": "spec.nodeName", "api-version": "v1"}
                            },
                            "POD_IP": {
                                "field": {"path": "status.podIP", "api-version": "v1"}
                            },
                            "CSI_ENDPOINT": "unix://{}".format(
                                csi_socket.get("container")
                            ),
                        },
                    },
                ],
            },
            k8s_resources={
                "kubernetesResources": {
                    "serviceAccounts": [
                        {
                            "name": "cephfs-csi-nodeplugin",
                            "roles": [
                                {
                                    "name": "cephfs-csi-nodeplugin",
                                    "global": True,
                                    "rules": [
                                        {
                                            "apiGroups": [""],
                                            "resources": ["nodes"],
                                            "verbs": ["get"],
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                }
            },
        )
        self.model.unit.status = ActiveStatus()


if __name__ == "__main__":
    main(CephCsiCharm)
