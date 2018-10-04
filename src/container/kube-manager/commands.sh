# edit /etc/contrail/kubemanager/docker-compose.sh
# replace entrypoint with sth e.g. sleep 5000
# mount volumes:
#    volumes:
#      - /var/log/contrail:/var/log/contrail
#      - /tmp/Juniper/contrail-controller/src/container/kube-manager/kube_manager/vnc:/lib/python2.7/site-packages/kube_manager/vnc
#      - /tmp/Juniper/contrail-controller/src/container/kube-manager/kube_manager/common:/lib/python2.7/site-packages/kube_manager/common
#      - /tmp/Juniper/contrail-controller/src/config/common/vnc_etcd.py:/lib/python2.7/site-packages/cfgm_common/vnc_etcd.py
#      - /tmp/Juniper/contrail-controller/src/config/common/vnc_object_db.py:/lib/python2.7/site-packages/cfgm_common/vnc_object_db.py

# Get pip and install etcd3
curl "https://bootstrap.pypa.io/get-pip.py" -o "get-pip.py"
python get-pip.py
pip install etcd3

# On container
# create config file
.//entrypoint.sh
# run kube-manager
/usr/bin/python /usr/bin/contrail-kube-manager -c /etc/contrail/contrail-kubernetes.conf
