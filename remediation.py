import time
import boto3
import subprocess
import tempfile
import os
import kubernetes
def get_instance_id_by_node(ec2_client, node_name):
    try:
        resp = ec2_client.describe_instances(
            Filters=[{"Name": "private-dns-name", "Values": [node_name]}]
        )
        return resp["Reservations"][0]["Instances"][0]["InstanceId"]
    except Exception as e:
        print(f"[ERROR] Could not find instance ID for {node_name}: {e}")
        return None


def remediate_kubelet_config(region, non_compliant_nodes, patch_command, session):
  
    ec2 = session.client("ec2", region_name=region)
    ssm = session.client("ssm", region_name=region)
    all_success = True

    for node_name in non_compliant_nodes.keys():
        # Step 1: Get EC2 instance ID
        instance_id = get_instance_id_by_node(ec2, node_name)
        if not instance_id:
            print(f"[SKIP] Could not resolve EC2 instance ID: {node_name}")
            all_success = False
            continue

        # Step 2: Check if SSM is available
        try:
            info = ssm.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
            )
            if not info["InstanceInformationList"]:
                print(f"[SKIP] {instance_id} is not managed by SSM, skipping")
                all_success = False
                continue
        except Exception as e:
            print(f"[ERROR] SSM query failed: {instance_id}: {e}")
            all_success = False
            continue

        # Step 3: Execute remediation command (modify kubelet-config.json and restart kubelet)
        patch_and_restart_cmd = f"""
CONFIG=/etc/kubernetes/kubelet/kubelet-config.json
if [ -f "$CONFIG" ]; then
  jq '{patch_command}' "$CONFIG" > /tmp/tmp-kubelet-config.json && \
  mv /tmp/tmp-kubelet-config.json "$CONFIG" && \
  systemctl daemon-reload && \
  systemctl restart kubelet
else
  echo "Config not found" && exit 1
fi
"""

        try:
            res = ssm.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={'commands': [patch_and_restart_cmd]},
            )
            command_id = res["Command"]["CommandId"]
            time.sleep(2)

            result = ssm.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id
            )

            if result["Status"] != "Success":
                print(f"[ERROR] Remediation failed on {node_name}: {result['StandardErrorContent']}")
                all_success = False
        except Exception as e:
            print(f"[ERROR] SSM command failed for {node_name}: {e}")
            all_success = False
    
    return all_success

def remediate_file_permissions(cluster_name, region, non_compliant_nodes, operation, value, profile):

    all_success = True
   
    for node_name, file_paths in non_compliant_nodes.items():
        temp_file_path = None
        try:
            subprocess.run(
                f"aws eks update-kubeconfig --name {cluster_name} --region {region} --profile {profile}",
                shell=True, check=True)
            
            pod_yaml = (
                "apiVersion: v1\n"
                "kind: Pod\n"
                "metadata:\n"
                "  name: file-check\n"
                "  namespace: default\n"
                "spec:\n"
                f"  nodeName: {node_name}\n"
                "  volumes:\n"
                "  - name: host-root\n"
                "    hostPath:\n"
                "      path: /\n"
                "      type: Directory\n"
                "  containers:\n"
                "  - name: nsenter\n"
                "    image: busybox\n"
                "    command: [\"sleep\", \"3600\"]\n"
                "    volumeMounts:\n"
                "    - name: host-root\n"
                "      mountPath: /host\n"
                "    securityContext:\n"
                "      privileged: true\n"
                "      runAsUser: 0\n"
                "  tolerations:\n"
                "  - effect: NoSchedule\n"
                "    operator: Exists\n"
            )
            
            with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as temp_file:
                temp_file.write(pod_yaml)
                temp_file_path = temp_file.name
            
            create_pod_cmd = f"kubectl apply -f {temp_file_path}"
            subprocess.run(create_pod_cmd, shell=True, check=True)
            
            subprocess.run("kubectl wait --for=condition=Ready pod/file-check --timeout=60s",
                         shell=True, check=True)
            
            for file_path in file_paths:
                cmd = f'kubectl exec -it file-check -- {operation} {value} {file_path}'
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True)

                if result.returncode != 0:
                    print(f"Failed to remediate {file_path}: {result.stderr}")
                    all_success = False
                
        finally:
            delete_cmd = "kubectl delete pod file-check --ignore-not-found"
            subprocess.run(delete_cmd, shell=True, check=True)

            wait_cmd = "kubectl wait --for=delete pod/file-check --timeout=60s || true"
            subprocess.run(wait_cmd, shell=True)

            if temp_file_path and os.path.exists(temp_file_path):
                os.unlink(temp_file_path)

    return all_success

def remediate_cis_2_1_1(client, cluster_name):
    print(f"Remediating CIS 2.1.1 for cluster {cluster_name}")
    try:
        # Enable all logging types
        client.update_cluster_config(
            name=cluster_name,
            logging={
                'clusterLogging': [
                    {
                        'types': [
                            'api',
                            'audit',
                            'authenticator',
                            'controllerManager',
                            'scheduler'
                        ],
                        'enabled': True
                    }
                ]
            }
        )
        return True
    except Exception as cluster_error:
        print(f"Failed to update cluster {cluster_name}: {str(cluster_error)}")
        return False

def remediate_cis_3_1_1(cluster_name, region, non_compliant_nodes, profile):
    return remediate_file_permissions(cluster_name, region, non_compliant_nodes, 'chmod', '644', profile)

def remediate_cis_3_1_2(cluster_name, region, non_compliant_nodes, profile):
    return remediate_file_permissions(cluster_name, region, non_compliant_nodes, 'chown', 'root:root', profile)

def remediate_cis_3_1_3(cluster_name, region, non_compliant_nodes, profile):
    return remediate_file_permissions(cluster_name, region, non_compliant_nodes, 'chmod', '644', profile)

def remediate_cis_3_1_4(cluster_name, region, non_compliant_nodes, profile):
    return remediate_file_permissions(cluster_name, region, non_compliant_nodes, 'chown', 'root:root', profile)

def remediate_cis_3_2_1(region, non_compliant_nodes, session):
    patch_command = '.authentication.anonymous.enabled = false'
    return remediate_kubelet_config(region, non_compliant_nodes, patch_command, session)

def remediate_cis_3_2_2(region, non_compliant_nodes, session):
    patch_command = '.authentication.webhook.enabled = true | .authorization.mode = "Webhook"'
    return remediate_kubelet_config(region, non_compliant_nodes, patch_command, session)

def remediate_cis_3_2_3(region, non_compliant_nodes, session):
    ca_file_path = "/etc/kubernetes/pki/ca.crt" 
    patch_command = f'.authentication.x509.clientCAFile = "{ca_file_path}"'
    return remediate_kubelet_config(region, non_compliant_nodes, patch_command, session)

def remediate_cis_3_2_4(region, non_compliant_nodes, session):
    patch_command = '.readOnlyPort = 0'
    return remediate_kubelet_config(region, non_compliant_nodes, patch_command, session)

def remediate_cis_3_2_5(region, non_compliant_nodes, session):
    patch_command = '.streamingConnectionIdleTimeout = "4h0m0s"'
    return remediate_kubelet_config(region, non_compliant_nodes, patch_command, session)

def remediate_cis_3_2_6(region, non_compliant_nodes, session):
    patch_command = '.makeIPTablesUtilChains = true'
    return remediate_kubelet_config(region, non_compliant_nodes, patch_command, session)

def remediate_cis_3_2_7(region, non_compliant_nodes, session):
    patch_command = '.eventRecordQPS = 5'
    return remediate_kubelet_config(region, non_compliant_nodes, patch_command, session)

def remediate_cis_3_2_8(region, non_compliant_nodes, session):
    patch_command = '.RotateCertificates = true'
    return remediate_kubelet_config(region, non_compliant_nodes, patch_command, session)

def remediate_cis_3_2_9(region, non_compliant_nodes, session):
    patch_command = '.featureGates.RotateKubeletServerCertificate = true'
    return remediate_kubelet_config(region, non_compliant_nodes, patch_command, session)

def remediate_cis_4_1_1(bindings, enable_fix=False):

    if enable_fix:
        success = True
        for binding in bindings:
            try:
                subprocess.run(["kubectl", "delete", "clusterrolebinding", binding], check=True)
            except subprocess.CalledProcessError:
                success = False 
        return success  
    else:
        print("CIS 4.1.1: Fix not enabled. Skipping remediation.")
        return True  

def remediate_cis_4_1_2(cluster_name):
    return f"For cluster {cluster_name}, please check and remove get, list and watch access to secret objects in the cluster"

def remediate_cis_4_1_3(cluster_name):
    return f"For cluster {cluster_name}, replace wildcard (*) permissions with specific apiGroups, resources, and verbs in Roles and ClusterRoles. Review each role with wildcard permissions and limit them to only what is necessary for the application or user."

def remediate_cis_4_1_4(cluster_name):
    return f"For cluster {cluster_name}, please check and remove create access to pod objects in the cluster"

def remediate_cis_4_1_5(enable_fix=False):

    if not enable_fix:
        print("CIS 4.1.5: Fix not enabled. Skipping remediation.")
        return True
    
    try:
        namespaces = subprocess.run(
            "kubectl get namespaces -o jsonpath='{.items[*].metadata.name}'",
            shell=True, check=True, capture_output=True, text=True
        ).stdout.strip().split()

        all_success = True  

        for ns in namespaces:
            patch_cmd = f"kubectl patch serviceaccount default -n {ns} -p '{{\"automountServiceAccountToken\": false}}'"
            try:
                subprocess.run(patch_cmd, shell=True, check=True)
            except subprocess.CalledProcessError:
                print(f"Failed to patch ServiceAccount in namespace: {ns}")
                all_success = False  
        
        return all_success  
    except Exception as e:
        print(f"Error fixing CIS 4.1.5: {str(e)}")
        return False  

def remediate_cis_4_1_6(cluster_name):
    return f"For cluster {cluster_name}, Regularly review pod and service account objects in the cluster to ensure that the automountServiceAccountToken setting is false for pods and accounts that do not explicitly require API server access."

def remediate_cis_4_1_7(cluster_name):
    return f"For cluster {cluster_name}, if the EKS cluster has already been created using ConfigMap, you will need to rebuild the cluster and choose EKS API for authentication."

def remediate_cis_4_1_8(cluster_name):
    return f"For cluster {cluster_name}, please check and remove the impersonate, bind and escalate rights from subjects."

def remediate_cis_4_2_x(cluster_name, non_compliant_pods, enable_fix=False):

    if not enable_fix:
        print("CIS 4.2.x: Fix not enabled. Skipping remediation.")
        return True
    
    all_success = True
    
    try:
        # Apply PSA policy to each namespace with user workloads
        for namespace in non_compliant_pods.keys():
            try:
                # Apply restricted policy to the namespace
                label_cmd = f"kubectl label --overwrite ns {namespace} pod-security.kubernetes.io/enforce=restricted"
                subprocess.run(label_cmd, shell=True, check=True)
                
            except subprocess.CalledProcessError as e:
                print(f"Failed to apply PSA policy for namespace {namespace}: {str(e)}")
                all_success = False
        
        return all_success
    except Exception as e:
        print(f"Error fixing CIS 4.2.1: {str(e)} for cluster {cluster_name}")
        return False

def remediate_cis_4_3_1(cluster_name, region, profile):

    subprocess.run(
        f"aws eks update-kubeconfig --name {cluster_name} --region {region} --profile {profile}",
        shell=True, check=True
    )
    yaml_content = (
    "apiVersion: networking.k8s.io/v1\n"
    "kind: NetworkPolicy\n"
    "metadata:\n"
    "  name: deny-all\n"
    "  namespace: secure-policy\n"
    "spec:\n"
    "  podSelector: {}\n"
    "  policyTypes:\n"
    "    - Ingress\n"
    "    - Egress\n"
    )

    try:
        subprocess.run(
            ['kubectl', 'create', 'namespace', 'secure-policy'],
            capture_output=True,
            text=True
        )

        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.yaml') as tmp_file:
            tmp_file.write(yaml_content)
            tmp_file_path = tmp_file.name

        subprocess.run(
            ['kubectl', 'apply', '-f', tmp_file_path],
            text=True,
            capture_output=True,
            check=True
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error fixing CIS 4.3.1: {e.stderr.strip()} for cluster {cluster_name}")
        return False
    finally:
        if os.path.exists(tmp_file_path):
            os.remove(tmp_file_path)

def remediate_cis_4_3_2(cluster_name, region, noncompliant_namespaces, profile, enable_fix=False):

    if not enable_fix:
        print("CIS 4.3.2: Fix not enabled. Skipping remediation.")
        return True

    all_success = True

    subprocess.run(
        f"aws eks update-kubeconfig --name {cluster_name} --region {region} --profile {profile}",
        shell=True, check=True
    )

    for namespace in noncompliant_namespaces:
        yaml_content = (
        f"apiVersion: networking.k8s.io/v1\n"
        f"kind: NetworkPolicy\n"
        f"metadata:\n"
        f"  name: deny-all\n"
        f"  namespace: {namespace}\n"
        f"spec:\n"
        f"  podSelector: {{}}\n"
        f"  policyTypes:\n"
        f"    - Ingress\n"
        f"    - Egress\n"
        )

        try:
            with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.yaml') as tmp_file:
                tmp_file.write(yaml_content)
                tmp_file_path = tmp_file.name

            subprocess.run(
                ['kubectl', 'apply', '-f', tmp_file_path],
                text=True,
                capture_output=True,
                check=True
            )
        
        except subprocess.CalledProcessError as e:
            print(f"Error fixing CIS 4.3.2: {e.stderr.strip()} for cluster {cluster_name}")
            all_success = False

        finally:
            if os.path.exists(tmp_file_path):
                os.remove(tmp_file_path)
    
    return all_success

def remediate_cis_4_4_1(cluster_name):
    return (
        "To remediate CIS 4.4.1: Identify resources using secrets via environment variables "
        "and modify them to use mounted secret files instead. Avoid using `env.valueFrom.secretKeyRef`, "
        "and instead mount the secret using `volumes` and `volumeMounts`, "
        "so your application reads secrets from the filesystem."
    )

def remediate_cis_4_5_1(cluster_name):
    return (
        "To remediate CIS 4.5.1: Create additional namespaces in your cluster to separate workloads. "
        "Avoid placing all workloads in the 'default' namespace. Use 'kubectl create namespace <name>' "
        "and assign resources explicitly to appropriate namespaces in your deployment manifests."
    )

def remediate_cis_4_5_2(cluster_name, region, resource_dict, profile, target_namespace='secure-app', enable_fix=False):

    if not enable_fix:
        print("CIS 4.5.2: Fix not enabled. Skipping remediation.")
        return True

    all_success = True

    subprocess.run(
        f"aws eks update-kubeconfig --name {cluster_name} --region {region} --profile {profile}",
        shell=True, check=True
    )

    subprocess.run(
        ['kubectl', 'create', 'namespace', target_namespace],
        capture_output=True, text=True
    )

    for kind, names in resource_dict.items():
        for full_name in names:
            try:
                name = full_name.split('/')[-1]
                
                export = subprocess.run(
                    ['kubectl', 'get', kind, name, '-n', 'default', '-o', 'yaml'],
                    capture_output=True, text=True, check=True
                )
                yaml_content = export.stdout.replace('namespace: default', f'namespace: {target_namespace}')

                with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.yaml') as tmp:
                    tmp.write(yaml_content)
                    tmp_path = tmp.name

                subprocess.run(['kubectl', 'apply', '-f', tmp_path], check=True)

                subprocess.run(['kubectl', 'delete', kind, name, '-n', 'default'], check=True)

            except subprocess.CalledProcessError as e:
                print(f"Error fixing CIS 4.5.2: {e.stderr.strip()} for cluster {cluster_name}")
                all_success = False
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

    return all_success

def remediate_5_1_1(session, non_compliant_repos):

    all_success = True
    ecr_client = session.client("ecr")
    for repo in non_compliant_repos:
        try:
            ecr_client.put_image_scanning_configuration(
                repositoryName=repo,
                imageScanningConfiguration={
                    'scanOnPush': True
                }
            )
            
        except Exception as e:
            print(f"Failed to remediate repository '{repo}': {e}")
            all_success = False

    return all_success

def remediate_cis_5_1_2(cluster_name):
    return (
        f"For cluster {cluster_name},"
        "Avoid attaching ECR write/delete policies to IAM users."
        "Detach policies such as AmazonEC2ContainerRegistryFullAccess from IAM users."
        "Instead, use IAM roles with scoped access and attach those to services or automation."
        "Recommended minimum actions for users (if any): ecr:GetDownloadUrlForLayer, ecr:BatchGetImage, ecr:GetAuthorizationToken\n"
    )

def remediate_cis_5_1_3(cluster_name):
    return (
        f"For cluster {cluster_name},"
        "Limit EKS control and node IAM roles to read-only access to Amazon ECR."
        "Avoid permissions such as ecr:PutImage, ecr:DeleteRepository, ecr:BatchDeleteImage, etc."
        "Recommended policy: AmazonEC2ContainerRegistryReadOnly \n"
    )

def remediate_cis_5_1_4(cluster_name):
    return (
        f"For cluster {cluster_name},"
        "Only use container images from trusted registries such as AWS ECR or official Docker Hub."
        "Please confirm whether all container images are from trusted registries."
    )

def remediate_5_2_1(kconfig, details, enable_fix=False):

    if not enable_fix:
        print("CIS 5.2.1: Fix not enabled. Skipping remediation.")
        return True

    kclient = kubernetes.client.ApiClient(configuration=kconfig)
    core = kubernetes.client.CoreV1Api(api_client=kclient)
    rbac = kubernetes.client.RbacAuthorizationV1Api(api_client=kclient)

    for item in details:
        ns = item["namespace"]
        issue = item["issue"]

        if "automountServiceAccountToken" in issue:
            patch = {"automountServiceAccountToken": False}
            core.patch_namespaced_service_account(name="default", namespace=ns, body=patch)
            print(f" Patched automountServiceAccountToken to false in namespace: {ns}")

        if "Bound to RoleBinding" in issue:
            rb_name = issue.split(":")[-1].strip()
            rbac.delete_namespaced_role_binding(name=rb_name, namespace=ns)
            print(f" Deleted RoleBinding {rb_name} in namespace: {ns}")

    return True
    
def remediate_cis_5_3_1(cluster_name):
    return (
        f"For cluster {cluster_name},"
        "Enable Kubernetes secrets encryption using a customer-managed KMS key during EKS cluster creation."
        "This cannot be enabled after cluster is created."
        "Refer to: https://docs.aws.amazon.com/eks/latest/userguide/encrypt-secrets.html \n"
    )

def remediate_5_4_1(session, cluster_name, enable_fix=False):

    if not enable_fix:
        print("CIS 5.4.1: Fix not enabled. Skipping remediation.")
        return True

    eks_client = session.client("eks")

    try:
        eks_client.update_cluster_config(
            name=cluster_name,
            resourcesVpcConfig={
                "endpointPrivateAccess": True,
                "endpointPublicAccess": False
            }
        )
        
        return True
    except Exception as e:
        print(f" Remediation failed: {str(e)}")
        return False

def remediate_5_4_2(session, cluster_name, my_ip_cidr="", enable_fix=False):

    if not enable_fix:
        print("CIS 5.4.2: Fix not enabled. Skipping remediation.")
        return True
    
    if not my_ip_cidr:
        print("No IP CIDR provided. Skipping remediation to avoid blocking user.")
        return True
    
    try:
        eks_client = session.client("eks")

        eks_client.update_cluster_config(
            name=cluster_name,
            resourcesVpcConfig={
                'endpointPrivateAccess': True,
                'endpointPublicAccess': True,
                'publicAccessCidrs': [my_ip_cidr]
            }
        )

    except Exception as e:
        print(f" Remediation failed: {str(e)}")
        return False
    
    return True

def remediate_5_4_3(session, cluster_name, my_ip_cidr="", enable_fix=False):

    if not enable_fix:
        print("CIS 5.4.3: Fix not enabled. Skipping remediation.")
        return True
    
    try:
        eks_client = session.client("eks")
        eks_client.update_cluster_config(
            name=cluster_name,
            resourcesVpcConfig={
                "endpointPrivateAccess": True,
                "endpointPublicAccess": True,
                "publicAccessCidrs": [my_ip_cidr]  
            }
        )
        return True

    except Exception as e:
        print(f" Remediation failed: {str(e)}")
        return False
    
def remediate_5_4_4(session, cluster_name):

    try:
        eks_client = session.client("eks")
        eks_client.update_addon(
            clusterName=cluster_name,
            addonName="vpc-cni",
            configurationValues="{\"enableNetworkPolicy\":\"true\"}"
        )
        return True

    except Exception as e:
        print(f" Remediation failed: {str(e)}")
        return False
    
def remediate_cis_5_4_5(cluster_name):
    return (
        f"For cluster {cluster_name},"
        "Ensure your LoadBalancer services are using port 443 and configured with valid TLS certificates."
        "In AWS, use HTTPS listeners with ACM certificates and configure your Ingress or Service annotations accordingly."
            "See: https://docs.aws.amazon.com/elasticloadbalancing/latest/userguide/data-protection.html \n"
    )

def remediate_cis_5_5_1(cluster_name):
    return (
        f"For cluster {cluster_name},"
        "Ensure all Kubernetes RBAC users and groups are mapped through AWS IAM Authenticator."
        "Avoid using local Kubernetes Users or Groups in RoleBindings or ClusterRoleBindings."
        "Use 'aws-auth' ConfigMap to assign IAM identities with appropriate roles.\n"
    )

