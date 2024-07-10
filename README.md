# VAST Data CSI Driver

Usage
Helm must be installed to use the charts. Please refer to Helm’s documentation to get started.

Once Helm has been set up correctly, add the repo as follows:

helm repo add vastcsi https://zachjburke.github.io/vast-csi
If you had already added this repo earlier, run helm repo update to retrieve the latest versions of the packages. You can then run helm search repo vastcsi to see the charts.

To install the chart:

helm install my-chart-name zachjburke/vastcsi
To uninstall the chart:

helm delete my-chart-name
