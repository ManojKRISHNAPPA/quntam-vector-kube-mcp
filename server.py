#!/usr/bin/env python3
"""
Kubernetes MCP Server for Claude Desktop
Comprehensive Kubernetes + EKS management tools
Reads config from ~/.kube/config
"""

import json
import yaml
import os
import sys
import subprocess
from pathlib import Path
from typing import Any, Optional

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server
from mcp.server.models import InitializationOptions

# Kubernetes
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException
from kubernetes.config.config_exception import ConfigException

# AWS
try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError, ProfileNotFound
    AWS_AVAILABLE = True
except ImportError:
    AWS_AVAILABLE = False

KUBE_CONFIG_PATH = str(Path.home() / ".kube" / "config")

app = Server("kubernetes-mcp")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_config(context: Optional[str] = None) -> None:
    try:
        config.load_kube_config(config_file=KUBE_CONFIG_PATH, context=context)
    except ConfigException as e:
        raise RuntimeError(f"Failed to load kubeconfig: {e}")


def _clients(context: Optional[str] = None):
    _load_config(context)
    return {
        "core":        client.CoreV1Api(),
        "apps":        client.AppsV1Api(),
        "batch":       client.BatchV1Api(),
        "networking":  client.NetworkingV1Api(),
        "rbac":        client.RbacAuthorizationV1Api(),
        "storage":     client.StorageV1Api(),
        "autoscaling": client.AutoscalingV2Api(),
        "custom":      client.CustomObjectsApi(),
        "version":     client.VersionApi(),
        "policy":      client.PolicyV1Api(),
    }


def _ok(data: Any) -> list[types.TextContent]:
    if isinstance(data, str):
        return [types.TextContent(type="text", text=data)]
    return [types.TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def _err(msg: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=f"ERROR: {msg}")]


def _safe_dict(obj) -> dict:
    """Convert k8s object to serialisable dict via JSON round-trip."""
    from kubernetes.client import ApiClient
    ac = ApiClient()
    return ac.sanitize_for_serialization(obj)


def _ns(args: dict) -> str:
    return args.get("namespace", "default")


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOLS = [
    # ── Context / Config ─────────────────────────────────────────────────
    types.Tool(
        name="k8s_get_contexts",
        description="List all kubeconfig contexts from ~/.kube/config",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    types.Tool(
        name="k8s_get_current_context",
        description="Get the current active kubeconfig context",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    types.Tool(
        name="k8s_switch_context",
        description="Switch the current kubeconfig context",
        inputSchema={
            "type": "object",
            "properties": {
                "context": {"type": "string", "description": "Context name to switch to"},
            },
            "required": ["context"],
        },
    ),
    types.Tool(
        name="k8s_cluster_info",
        description="Get cluster server URL and version info",
        inputSchema={
            "type": "object",
            "properties": {
                "context": {"type": "string", "description": "Optional context name"},
            },
        },
    ),

    # ── Namespaces ───────────────────────────────────────────────────────
    types.Tool(
        name="k8s_list_namespaces",
        description="List all namespaces in the cluster",
        inputSchema={
            "type": "object",
            "properties": {
                "context": {"type": "string"},
            },
        },
    ),
    types.Tool(
        name="k8s_create_namespace",
        description="Create a new namespace",
        inputSchema={
            "type": "object",
            "properties": {
                "name":    {"type": "string"},
                "context": {"type": "string"},
            },
            "required": ["name"],
        },
    ),
    types.Tool(
        name="k8s_delete_namespace",
        description="Delete a namespace (CAUTION: deletes everything in it)",
        inputSchema={
            "type": "object",
            "properties": {
                "name":    {"type": "string"},
                "context": {"type": "string"},
            },
            "required": ["name"],
        },
    ),

    # ── Generic resource operations ──────────────────────────────────────
    types.Tool(
        name="k8s_list_resources",
        description=(
            "List any Kubernetes resource. resource_type examples: "
            "pods, deployments, services, replicasets, statefulsets, "
            "daemonsets, jobs, cronjobs, configmaps, secrets, pvcs, pv, "
            "ingresses, networkpolicies, serviceaccounts, roles, "
            "rolebindings, clusterroles, clusterrolebindings, nodes, events"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "resource_type": {"type": "string"},
                "namespace":     {"type": "string", "description": "omit for cluster-scoped resources"},
                "label_selector":{"type": "string", "description": "e.g. app=nginx"},
                "all_namespaces":{"type": "boolean", "default": False},
                "context":       {"type": "string"},
            },
            "required": ["resource_type"],
        },
    ),
    types.Tool(
        name="k8s_describe_resource",
        description="Describe a Kubernetes resource (equivalent to kubectl describe)",
        inputSchema={
            "type": "object",
            "properties": {
                "resource_type": {"type": "string"},
                "name":          {"type": "string"},
                "namespace":     {"type": "string"},
                "context":       {"type": "string"},
            },
            "required": ["resource_type", "name"],
        },
    ),
    types.Tool(
        name="k8s_get_resource_yaml",
        description="Get full YAML manifest of a resource",
        inputSchema={
            "type": "object",
            "properties": {
                "resource_type": {"type": "string"},
                "name":          {"type": "string"},
                "namespace":     {"type": "string"},
                "context":       {"type": "string"},
            },
            "required": ["resource_type", "name"],
        },
    ),
    types.Tool(
        name="k8s_delete_resource",
        description="Delete a Kubernetes resource by type and name",
        inputSchema={
            "type": "object",
            "properties": {
                "resource_type": {"type": "string"},
                "name":          {"type": "string"},
                "namespace":     {"type": "string"},
                "force":         {"type": "boolean", "default": False},
                "context":       {"type": "string"},
            },
            "required": ["resource_type", "name"],
        },
    ),
    types.Tool(
        name="k8s_apply_manifest",
        description="Apply a Kubernetes manifest (YAML or JSON string) – create or update",
        inputSchema={
            "type": "object",
            "properties": {
                "manifest": {"type": "string", "description": "YAML or JSON manifest"},
                "namespace": {"type": "string"},
                "context":   {"type": "string"},
            },
            "required": ["manifest"],
        },
    ),

    # ── Pods ─────────────────────────────────────────────────────────────
    types.Tool(
        name="k8s_get_pod_logs",
        description="Fetch logs from a pod (optionally a specific container)",
        inputSchema={
            "type": "object",
            "properties": {
                "pod_name":      {"type": "string"},
                "namespace":     {"type": "string"},
                "container":     {"type": "string"},
                "tail_lines":    {"type": "integer", "default": 100},
                "previous":      {"type": "boolean", "default": False, "description": "Get logs of previous crashed container"},
                "context":       {"type": "string"},
            },
            "required": ["pod_name"],
        },
    ),
    types.Tool(
        name="k8s_pod_events",
        description="Get events for a specific pod",
        inputSchema={
            "type": "object",
            "properties": {
                "pod_name":  {"type": "string"},
                "namespace": {"type": "string"},
                "context":   {"type": "string"},
            },
            "required": ["pod_name"],
        },
    ),

    # ── Deployments ──────────────────────────────────────────────────────
    types.Tool(
        name="k8s_scale_deployment",
        description="Scale a deployment to the given replica count",
        inputSchema={
            "type": "object",
            "properties": {
                "name":      {"type": "string"},
                "replicas":  {"type": "integer"},
                "namespace": {"type": "string"},
                "context":   {"type": "string"},
            },
            "required": ["name", "replicas"],
        },
    ),
    types.Tool(
        name="k8s_rollout_status",
        description="Get rollout status of a deployment / statefulset / daemonset",
        inputSchema={
            "type": "object",
            "properties": {
                "resource_type": {"type": "string", "default": "deployment"},
                "name":          {"type": "string"},
                "namespace":     {"type": "string"},
                "context":       {"type": "string"},
            },
            "required": ["name"],
        },
    ),
    types.Tool(
        name="k8s_rollout_restart",
        description="Restart (rolling) a deployment / statefulset / daemonset",
        inputSchema={
            "type": "object",
            "properties": {
                "resource_type": {"type": "string", "default": "deployment"},
                "name":          {"type": "string"},
                "namespace":     {"type": "string"},
                "context":       {"type": "string"},
            },
            "required": ["name"],
        },
    ),
    types.Tool(
        name="k8s_rollout_history",
        description="Show rollout history of a deployment",
        inputSchema={
            "type": "object",
            "properties": {
                "name":      {"type": "string"},
                "namespace": {"type": "string"},
                "context":   {"type": "string"},
            },
            "required": ["name"],
        },
    ),
    types.Tool(
        name="k8s_rollout_undo",
        description="Rollback a deployment to a previous revision",
        inputSchema={
            "type": "object",
            "properties": {
                "name":      {"type": "string"},
                "namespace": {"type": "string"},
                "revision":  {"type": "integer", "description": "0 = previous revision"},
                "context":   {"type": "string"},
            },
            "required": ["name"],
        },
    ),

    # ── Nodes ────────────────────────────────────────────────────────────
    types.Tool(
        name="k8s_node_health",
        description="Check health of all nodes – conditions, capacity, allocatable resources, taints",
        inputSchema={
            "type": "object",
            "properties": {
                "node_name": {"type": "string", "description": "Optional: check a single node"},
                "context":   {"type": "string"},
            },
        },
    ),
    types.Tool(
        name="k8s_cordon_node",
        description="Cordon a node (mark unschedulable)",
        inputSchema={
            "type": "object",
            "properties": {
                "node_name": {"type": "string"},
                "context":   {"type": "string"},
            },
            "required": ["node_name"],
        },
    ),
    types.Tool(
        name="k8s_uncordon_node",
        description="Uncordon a node (mark schedulable)",
        inputSchema={
            "type": "object",
            "properties": {
                "node_name": {"type": "string"},
                "context":   {"type": "string"},
            },
            "required": ["node_name"],
        },
    ),
    types.Tool(
        name="k8s_drain_node",
        description="Drain a node (evict all pods, ignore daemonsets)",
        inputSchema={
            "type": "object",
            "properties": {
                "node_name":            {"type": "string"},
                "ignore_daemonsets":    {"type": "boolean", "default": True},
                "delete_emptydir_data": {"type": "boolean", "default": False},
                "context":              {"type": "string"},
            },
            "required": ["node_name"],
        },
    ),

    # ── Events ───────────────────────────────────────────────────────────
    types.Tool(
        name="k8s_get_events",
        description="Get cluster events, optionally filtered by namespace or resource",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace":      {"type": "string"},
                "all_namespaces": {"type": "boolean", "default": False},
                "field_selector": {"type": "string", "description": "e.g. reason=BackOff"},
                "warnings_only":  {"type": "boolean", "default": False},
                "context":        {"type": "string"},
            },
        },
    ),

    # ── Top (resource usage) ─────────────────────────────────────────────
    types.Tool(
        name="k8s_top_nodes",
        description="Show node CPU/memory usage (requires metrics-server)",
        inputSchema={
            "type": "object",
            "properties": {
                "context": {"type": "string"},
            },
        },
    ),
    types.Tool(
        name="k8s_top_pods",
        description="Show pod CPU/memory usage (requires metrics-server)",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace":      {"type": "string"},
                "all_namespaces": {"type": "boolean", "default": False},
                "context":        {"type": "string"},
            },
        },
    ),

    # ── Secrets / ConfigMaps ─────────────────────────────────────────────
    types.Tool(
        name="k8s_get_secret",
        description="Get a secret's data (base64-decoded). Use carefully.",
        inputSchema={
            "type": "object",
            "properties": {
                "name":      {"type": "string"},
                "namespace": {"type": "string"},
                "decode":    {"type": "boolean", "default": True},
                "context":   {"type": "string"},
            },
            "required": ["name"],
        },
    ),
    types.Tool(
        name="k8s_create_secret",
        description="Create a generic secret from key=value pairs",
        inputSchema={
            "type": "object",
            "properties": {
                "name":      {"type": "string"},
                "namespace": {"type": "string"},
                "data":      {"type": "object", "description": "key: plaintext-value pairs"},
                "context":   {"type": "string"},
            },
            "required": ["name", "data"],
        },
    ),

    # ── EKS ──────────────────────────────────────────────────────────────
    types.Tool(
        name="eks_list_clusters",
        description="List all EKS clusters in an AWS region",
        inputSchema={
            "type": "object",
            "properties": {
                "region":  {"type": "string", "description": "AWS region, e.g. us-west-2"},
                "profile": {"type": "string", "description": "AWS profile name"},
            },
        },
    ),
    types.Tool(
        name="eks_cluster_info",
        description="Get EKS cluster details: version, status, endpoint, logging, networking",
        inputSchema={
            "type": "object",
            "properties": {
                "cluster_name": {"type": "string"},
                "region":       {"type": "string"},
                "profile":      {"type": "string"},
            },
            "required": ["cluster_name"],
        },
    ),
    types.Tool(
        name="eks_check_version",
        description="Check EKS Kubernetes version and compare with latest available",
        inputSchema={
            "type": "object",
            "properties": {
                "cluster_name": {"type": "string"},
                "region":       {"type": "string"},
                "profile":      {"type": "string"},
            },
            "required": ["cluster_name"],
        },
    ),
    types.Tool(
        name="eks_cluster_health",
        description="Check EKS cluster health: issues, addon health, control-plane connectivity",
        inputSchema={
            "type": "object",
            "properties": {
                "cluster_name": {"type": "string"},
                "region":       {"type": "string"},
                "profile":      {"type": "string"},
                "context":      {"type": "string", "description": "kubeconfig context to use"},
            },
            "required": ["cluster_name"],
        },
    ),
    types.Tool(
        name="eks_list_nodegroups",
        description="List EKS managed node groups for a cluster",
        inputSchema={
            "type": "object",
            "properties": {
                "cluster_name": {"type": "string"},
                "region":       {"type": "string"},
                "profile":      {"type": "string"},
            },
            "required": ["cluster_name"],
        },
    ),
    types.Tool(
        name="eks_nodegroup_health",
        description="Check health of EKS managed node groups (issues, scaling, instance types)",
        inputSchema={
            "type": "object",
            "properties": {
                "cluster_name":  {"type": "string"},
                "nodegroup_name":{"type": "string", "description": "Optional: check specific nodegroup"},
                "region":        {"type": "string"},
                "profile":       {"type": "string"},
            },
            "required": ["cluster_name"],
        },
    ),
    types.Tool(
        name="eks_list_addons",
        description="List EKS addons and their versions / health status",
        inputSchema={
            "type": "object",
            "properties": {
                "cluster_name": {"type": "string"},
                "region":       {"type": "string"},
                "profile":      {"type": "string"},
            },
            "required": ["cluster_name"],
        },
    ),
    types.Tool(
        name="eks_update_kubeconfig",
        description="Update ~/.kube/config with credentials for an EKS cluster",
        inputSchema={
            "type": "object",
            "properties": {
                "cluster_name": {"type": "string"},
                "region":       {"type": "string"},
                "profile":      {"type": "string"},
                "alias":        {"type": "string", "description": "Optional context alias"},
            },
            "required": ["cluster_name", "region"],
        },
    ),
    types.Tool(
        name="eks_fargate_profiles",
        description="List Fargate profiles for an EKS cluster",
        inputSchema={
            "type": "object",
            "properties": {
                "cluster_name": {"type": "string"},
                "region":       {"type": "string"},
                "profile":      {"type": "string"},
            },
            "required": ["cluster_name"],
        },
    ),
]


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        return await _dispatch(name, arguments)
    except ApiException as e:
        return _err(f"Kubernetes API error {e.status}: {e.reason}\n{e.body}")
    except RuntimeError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def _dispatch(name: str, args: dict) -> list[types.TextContent]:
    ctx = args.get("context")

    # ── Context / Config ─────────────────────────────────────────────────
    if name == "k8s_get_contexts":
        cfg = config.list_kube_config_contexts(config_file=KUBE_CONFIG_PATH)
        contexts = [c["name"] for c in (cfg[0] or [])]
        current = cfg[1]["name"] if cfg[1] else None
        return _ok({"current_context": current, "contexts": contexts})

    if name == "k8s_get_current_context":
        _, active = config.list_kube_config_contexts(config_file=KUBE_CONFIG_PATH)
        return _ok(active)

    if name == "k8s_switch_context":
        subprocess.run(
            ["kubectl", "config", "use-context", args["context"],
             "--kubeconfig", KUBE_CONFIG_PATH],
            check=True, capture_output=True
        )
        return _ok(f"Switched to context: {args['context']}")

    if name == "k8s_cluster_info":
        apis = _clients(ctx)
        ver = apis["version"].get_code()
        return _ok({
            "git_version":    ver.git_version,
            "major":          ver.major,
            "minor":          ver.minor,
            "platform":       ver.platform,
            "go_version":     ver.go_version,
            "build_date":     ver.build_date,
        })

    # ── Namespaces ───────────────────────────────────────────────────────
    if name == "k8s_list_namespaces":
        apis = _clients(ctx)
        nss = apis["core"].list_namespace()
        result = [
            {
                "name":   ns.metadata.name,
                "status": ns.status.phase,
                "age":    str(ns.metadata.creation_timestamp),
            }
            for ns in nss.items
        ]
        return _ok(result)

    if name == "k8s_create_namespace":
        apis = _clients(ctx)
        body = client.V1Namespace(metadata=client.V1ObjectMeta(name=args["name"]))
        ns = apis["core"].create_namespace(body)
        return _ok(f"Namespace '{ns.metadata.name}' created.")

    if name == "k8s_delete_namespace":
        apis = _clients(ctx)
        apis["core"].delete_namespace(args["name"])
        return _ok(f"Namespace '{args['name']}' deleted.")

    # ── Generic list ─────────────────────────────────────────────────────
    if name == "k8s_list_resources":
        return await _list_resources(args)

    # ── Describe ─────────────────────────────────────────────────────────
    if name == "k8s_describe_resource":
        return await _describe_resource(args)

    # ── Get YAML ─────────────────────────────────────────────────────────
    if name == "k8s_get_resource_yaml":
        return await _get_resource_yaml(args)

    # ── Delete ───────────────────────────────────────────────────────────
    if name == "k8s_delete_resource":
        return await _delete_resource(args)

    # ── Apply manifest ───────────────────────────────────────────────────
    if name == "k8s_apply_manifest":
        return await _apply_manifest(args)

    # ── Pod logs ─────────────────────────────────────────────────────────
    if name == "k8s_get_pod_logs":
        apis = _clients(ctx)
        ns   = _ns(args)
        logs = apis["core"].read_namespaced_pod_log(
            name=args["pod_name"],
            namespace=ns,
            container=args.get("container"),
            tail_lines=args.get("tail_lines", 100),
            previous=args.get("previous", False),
        )
        return _ok(logs)

    if name == "k8s_pod_events":
        apis = _clients(ctx)
        ns   = _ns(args)
        evts = apis["core"].list_namespaced_event(
            namespace=ns,
            field_selector=f"involvedObject.name={args['pod_name']}"
        )
        rows = []
        for e in evts.items:
            rows.append({
                "time":    str(e.last_timestamp or e.event_time),
                "type":    e.type,
                "reason":  e.reason,
                "message": e.message,
            })
        return _ok(rows)

    # ── Deployments ──────────────────────────────────────────────────────
    if name == "k8s_scale_deployment":
        apis = _clients(ctx)
        ns   = _ns(args)
        body = {"spec": {"replicas": args["replicas"]}}
        apis["apps"].patch_namespaced_deployment_scale(
            name=args["name"], namespace=ns, body=body
        )
        return _ok(f"Deployment '{args['name']}' scaled to {args['replicas']} replica(s).")

    if name == "k8s_rollout_status":
        return _kubectl_run(
            ["kubectl", "rollout", "status",
             f"{args.get('resource_type','deployment')}/{args['name']}",
             "-n", _ns(args)],
            ctx
        )

    if name == "k8s_rollout_restart":
        return _kubectl_run(
            ["kubectl", "rollout", "restart",
             f"{args.get('resource_type','deployment')}/{args['name']}",
             "-n", _ns(args)],
            ctx
        )

    if name == "k8s_rollout_history":
        return _kubectl_run(
            ["kubectl", "rollout", "history",
             f"deployment/{args['name']}",
             "-n", _ns(args)],
            ctx
        )

    if name == "k8s_rollout_undo":
        cmd = ["kubectl", "rollout", "undo", f"deployment/{args['name']}",
               "-n", _ns(args)]
        rev = args.get("revision")
        if rev:
            cmd += [f"--to-revision={rev}"]
        return _kubectl_run(cmd, ctx)

    # ── Node health ──────────────────────────────────────────────────────
    if name == "k8s_node_health":
        return await _node_health(args)

    if name == "k8s_cordon_node":
        return _kubectl_run(["kubectl", "cordon", args["node_name"]], ctx)

    if name == "k8s_uncordon_node":
        return _kubectl_run(["kubectl", "uncordon", args["node_name"]], ctx)

    if name == "k8s_drain_node":
        cmd = ["kubectl", "drain", args["node_name"], "--timeout=120s"]
        if args.get("ignore_daemonsets", True):
            cmd.append("--ignore-daemonsets")
        if args.get("delete_emptydir_data", False):
            cmd.append("--delete-emptydir-data")
        return _kubectl_run(cmd, ctx)

    # ── Events ───────────────────────────────────────────────────────────
    if name == "k8s_get_events":
        return await _get_events(args)

    # ── Top ──────────────────────────────────────────────────────────────
    if name == "k8s_top_nodes":
        return _kubectl_run(["kubectl", "top", "nodes"], ctx)

    if name == "k8s_top_pods":
        cmd = ["kubectl", "top", "pods"]
        if args.get("all_namespaces"):
            cmd.append("-A")
        elif "namespace" in args:
            cmd += ["-n", args["namespace"]]
        return _kubectl_run(cmd, ctx)

    # ── Secrets ──────────────────────────────────────────────────────────
    if name == "k8s_get_secret":
        apis = _clients(ctx)
        ns   = _ns(args)
        sec  = apis["core"].read_namespaced_secret(name=args["name"], namespace=ns)
        import base64
        result = {"name": sec.metadata.name, "namespace": ns, "type": sec.type, "data": {}}
        for k, v in (sec.data or {}).items():
            result["data"][k] = base64.b64decode(v).decode() if args.get("decode", True) else v
        return _ok(result)

    if name == "k8s_create_secret":
        import base64
        apis = _clients(ctx)
        ns   = _ns(args)
        encoded = {k: base64.b64encode(v.encode()).decode() for k, v in args["data"].items()}
        body = client.V1Secret(
            metadata=client.V1ObjectMeta(name=args["name"], namespace=ns),
            data=encoded,
        )
        apis["core"].create_namespaced_secret(namespace=ns, body=body)
        return _ok(f"Secret '{args['name']}' created in namespace '{ns}'.")

    # ── EKS ──────────────────────────────────────────────────────────────
    if name == "eks_list_clusters":
        return await _eks_list_clusters(args)

    if name == "eks_cluster_info":
        return await _eks_cluster_info(args)

    if name == "eks_check_version":
        return await _eks_check_version(args)

    if name == "eks_cluster_health":
        return await _eks_cluster_health(args)

    if name == "eks_list_nodegroups":
        return await _eks_list_nodegroups(args)

    if name == "eks_nodegroup_health":
        return await _eks_nodegroup_health(args)

    if name == "eks_list_addons":
        return await _eks_list_addons(args)

    if name == "eks_update_kubeconfig":
        return await _eks_update_kubeconfig(args)

    if name == "eks_fargate_profiles":
        return await _eks_fargate_profiles(args)

    return _err(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# Implementation helpers
# ---------------------------------------------------------------------------

def _kubectl_run(cmd: list[str], context: Optional[str] = None) -> list[types.TextContent]:
    if context:
        cmd += ["--context", context]
    cmd += ["--kubeconfig", KUBE_CONFIG_PATH]
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout + result.stderr
    return _ok(output.strip())


def _boto3_eks(args: dict):
    if not AWS_AVAILABLE:
        raise RuntimeError("boto3 is not installed. Run: pip install boto3")
    region  = args.get("region", "us-west-2")
    profile = args.get("profile")
    session = boto3.Session(region_name=region, profile_name=profile)
    return session.client("eks")


async def _list_resources(args: dict) -> list[types.TextContent]:
    ctx  = args.get("context")
    ns   = args.get("namespace", "default")
    rt   = args["resource_type"].lower().rstrip("s")  # normalise
    sel  = args.get("label_selector")
    all_ns = args.get("all_namespaces", False)
    apis = _clients(ctx)

    # Map common resource types to API calls
    NAMESPACED = {
        "pod":           lambda: apis["core"].list_namespaced_pod(namespace=ns, label_selector=sel),
        "deployment":    lambda: apis["apps"].list_namespaced_deployment(namespace=ns, label_selector=sel),
        "replicaset":    lambda: apis["apps"].list_namespaced_replica_set(namespace=ns, label_selector=sel),
        "statefulset":   lambda: apis["apps"].list_namespaced_stateful_set(namespace=ns, label_selector=sel),
        "daemonset":     lambda: apis["apps"].list_namespaced_daemon_set(namespace=ns, label_selector=sel),
        "service":       lambda: apis["core"].list_namespaced_service(namespace=ns, label_selector=sel),
        "endpoint":      lambda: apis["core"].list_namespaced_endpoints(namespace=ns),
        "configmap":     lambda: apis["core"].list_namespaced_config_map(namespace=ns, label_selector=sel),
        "secret":        lambda: apis["core"].list_namespaced_secret(namespace=ns, label_selector=sel),
        "pvc":           lambda: apis["core"].list_namespaced_persistent_volume_claim(namespace=ns),
        "serviceaccount":lambda: apis["core"].list_namespaced_service_account(namespace=ns),
        "job":           lambda: apis["batch"].list_namespaced_job(namespace=ns),
        "cronjob":       lambda: apis["batch"].list_namespaced_cron_job(namespace=ns),
        "ingress":       lambda: apis["networking"].list_namespaced_ingress(namespace=ns),
        "networkpolicy": lambda: apis["networking"].list_namespaced_network_policy(namespace=ns),
        "role":          lambda: apis["rbac"].list_namespaced_role(namespace=ns),
        "rolebinding":   lambda: apis["rbac"].list_namespaced_role_binding(namespace=ns),
        "event":         lambda: apis["core"].list_namespaced_event(namespace=ns),
        "hpa":           lambda: apis["autoscaling"].list_namespaced_horizontal_pod_autoscaler(namespace=ns),
        "pdb":           lambda: apis["policy"].list_namespaced_pod_disruption_budget(namespace=ns),
    }
    CLUSTER_SCOPED = {
        "node":               lambda: apis["core"].list_node(label_selector=sel),
        "namespace":          lambda: apis["core"].list_namespace(),
        "pv":                 lambda: apis["core"].list_persistent_volume(),
        "clusterrole":        lambda: apis["rbac"].list_cluster_role(),
        "clusterrolebinding": lambda: apis["rbac"].list_cluster_role_binding(),
        "storageclass":       lambda: apis["storage"].list_storage_class(),
    }

    if all_ns:
        ALL_NS = {
            "pod":        lambda: apis["core"].list_pod_for_all_namespaces(label_selector=sel),
            "deployment": lambda: apis["apps"].list_deployment_for_all_namespaces(label_selector=sel),
            "service":    lambda: apis["core"].list_service_for_all_namespaces(label_selector=sel),
            "event":      lambda: apis["core"].list_event_for_all_namespaces(),
            "job":        lambda: apis["batch"].list_job_for_all_namespaces(),
            "cronjob":    lambda: apis["batch"].list_cron_job_for_all_namespaces(),
        }
        fn = ALL_NS.get(rt)
        if not fn:
            return _err(f"all_namespaces not supported for '{rt}'. Try without it.")
        items = fn().items
    elif rt in CLUSTER_SCOPED:
        items = CLUSTER_SCOPED[rt]().items
    elif rt in NAMESPACED:
        items = NAMESPACED[rt]().items
    else:
        # Fall back to kubectl
        cmd = ["kubectl", "get", args["resource_type"], "-n", ns, "-o", "wide"]
        return _kubectl_run(cmd, ctx)

    rows = []
    for item in items:
        row = {
            "name":      item.metadata.name,
            "namespace": getattr(item.metadata, "namespace", None),
            "created":   str(item.metadata.creation_timestamp),
        }
        # Pod status
        if hasattr(item, "status") and hasattr(item.status, "phase"):
            row["phase"] = item.status.phase
        if hasattr(item, "status") and hasattr(item.status, "conditions"):
            conds = item.status.conditions or []
            ready = next((c for c in conds if c.type == "Ready"), None)
            if ready:
                row["ready"] = ready.status
        rows.append(row)
    return _ok(rows)


async def _describe_resource(args: dict) -> list[types.TextContent]:
    ctx = args.get("context")
    cmd = ["kubectl", "describe", args["resource_type"], args["name"]]
    if "namespace" in args:
        cmd += ["-n", args["namespace"]]
    return _kubectl_run(cmd, ctx)


async def _get_resource_yaml(args: dict) -> list[types.TextContent]:
    ctx = args.get("context")
    cmd = ["kubectl", "get", args["resource_type"], args["name"], "-o", "yaml"]
    if "namespace" in args:
        cmd += ["-n", args["namespace"]]
    return _kubectl_run(cmd, ctx)


async def _delete_resource(args: dict) -> list[types.TextContent]:
    ctx   = args.get("context")
    cmd   = ["kubectl", "delete", args["resource_type"], args["name"]]
    if "namespace" in args:
        cmd += ["-n", args["namespace"]]
    if args.get("force"):
        cmd += ["--force", "--grace-period=0"]
    return _kubectl_run(cmd, ctx)


async def _apply_manifest(args: dict) -> list[types.TextContent]:
    ctx      = args.get("context")
    manifest = args["manifest"]
    # write to temp file
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(manifest)
        fname = f.name
    try:
        cmd = ["kubectl", "apply", "-f", fname]
        if "namespace" in args:
            cmd += ["-n", args["namespace"]]
        return _kubectl_run(cmd, ctx)
    finally:
        os.unlink(fname)


async def _node_health(args: dict) -> list[types.TextContent]:
    ctx  = args.get("context")
    apis = _clients(ctx)
    node_name = args.get("node_name")
    if node_name:
        nodes = [apis["core"].read_node(node_name)]
    else:
        nodes = apis["core"].list_node().items

    result = []
    for node in nodes:
        conditions = {}
        for c in (node.status.conditions or []):
            conditions[c.type] = {"status": c.status, "reason": c.reason, "message": c.message}

        allocatable = node.status.allocatable or {}
        capacity    = node.status.capacity or {}

        healthy = conditions.get("Ready", {}).get("status") == "True"
        taints  = [
            f"{t.key}={t.value}:{t.effect}" if t.value else f"{t.key}:{t.effect}"
            for t in (node.spec.taints or [])
        ]

        result.append({
            "name":            node.metadata.name,
            "healthy":         healthy,
            "conditions":      conditions,
            "roles":           [
                k.split("/")[-1]
                for k in (node.metadata.labels or {})
                if k.startswith("node-role.kubernetes.io/")
            ],
            "instance_type":   (node.metadata.labels or {}).get("node.kubernetes.io/instance-type"),
            "os_image":        node.status.node_info.os_image if node.status.node_info else None,
            "kernel_version":  node.status.node_info.kernel_version if node.status.node_info else None,
            "kubelet_version": node.status.node_info.kubelet_version if node.status.node_info else None,
            "capacity": {
                "cpu":    capacity.get("cpu"),
                "memory": capacity.get("memory"),
                "pods":   capacity.get("pods"),
            },
            "allocatable": {
                "cpu":    allocatable.get("cpu"),
                "memory": allocatable.get("memory"),
                "pods":   allocatable.get("pods"),
            },
            "taints":          taints,
            "unschedulable":   node.spec.unschedulable or False,
        })
    return _ok(result)


async def _get_events(args: dict) -> list[types.TextContent]:
    ctx  = args.get("context")
    apis = _clients(ctx)
    ns   = args.get("namespace", "default")
    fsel = args.get("field_selector")
    warnings = args.get("warnings_only", False)

    if args.get("all_namespaces"):
        evts = apis["core"].list_event_for_all_namespaces(field_selector=fsel)
    else:
        evts = apis["core"].list_namespaced_event(namespace=ns, field_selector=fsel)

    rows = []
    for e in evts.items:
        if warnings and e.type == "Normal":
            continue
        rows.append({
            "time":      str(e.last_timestamp or e.event_time),
            "namespace": e.metadata.namespace,
            "type":      e.type,
            "reason":    e.reason,
            "object":    f"{e.involved_object.kind}/{e.involved_object.name}",
            "message":   e.message,
            "count":     e.count,
        })
    rows.sort(key=lambda r: str(r["time"]))
    return _ok(rows)


# ---------------------------------------------------------------------------
# EKS helpers
# ---------------------------------------------------------------------------

async def _eks_list_clusters(args: dict) -> list[types.TextContent]:
    eks = _boto3_eks(args)
    resp = eks.list_clusters()
    clusters = resp.get("clusters", [])
    return _ok({"clusters": clusters, "region": args.get("region", "us-west-2")})


async def _eks_cluster_info(args: dict) -> list[types.TextContent]:
    eks  = _boto3_eks(args)
    resp = eks.describe_cluster(name=args["cluster_name"])
    cl   = resp["cluster"]
    return _ok({
        "name":              cl.get("name"),
        "status":            cl.get("status"),
        "kubernetes_version":cl.get("version"),
        "endpoint":          cl.get("endpoint"),
        "role_arn":          cl.get("roleArn"),
        "vpc_config":        cl.get("resourcesVpcConfig"),
        "logging":           cl.get("logging"),
        "platform_version":  cl.get("platformVersion"),
        "created_at":        str(cl.get("createdAt")),
        "tags":              cl.get("tags"),
        "health":            cl.get("health"),
    })


async def _eks_check_version(args: dict) -> list[types.TextContent]:
    eks  = _boto3_eks(args)
    resp = eks.describe_cluster(name=args["cluster_name"])
    cl   = resp["cluster"]

    current_version = cl.get("version", "unknown")
    platform_version = cl.get("platformVersion", "unknown")
    status = cl.get("status")

    # List available versions from EKS
    try:
        versions_resp = eks.describe_addon_versions()
        k8s_versions = set()
        for addon in versions_resp.get("addons", []):
            for av in addon.get("addonVersions", []):
                for compat in av.get("compatibilities", []):
                    if "clusterVersion" in compat:
                        k8s_versions.add(compat["clusterVersion"])
        available_versions = sorted(k8s_versions, reverse=True)
    except Exception:
        available_versions = ["Unable to fetch"]

    latest = available_versions[0] if available_versions else "unknown"
    needs_upgrade = current_version != latest

    return _ok({
        "cluster_name":       args["cluster_name"],
        "current_k8s_version":current_version,
        "platform_version":   platform_version,
        "cluster_status":     status,
        "latest_available":   latest,
        "all_available":      available_versions[:10],
        "upgrade_recommended":needs_upgrade,
    })


async def _eks_cluster_health(args: dict) -> list[types.TextContent]:
    eks  = _boto3_eks(args)
    resp = eks.describe_cluster(name=args["cluster_name"])
    cl   = resp["cluster"]

    health_info = {
        "cluster_name":   args["cluster_name"],
        "cluster_status": cl.get("status"),
        "health_issues":  cl.get("health", {}).get("issues", []),
        "platform_version": cl.get("platformVersion"),
        "kubernetes_version": cl.get("version"),
        "logging_enabled": cl.get("logging", {}).get("clusterLogging", []),
    }

    # Also check addons health
    try:
        addons_resp = eks.list_addons(clusterName=args["cluster_name"])
        addon_health = []
        for addon_name in addons_resp.get("addons", []):
            try:
                ad = eks.describe_addon(clusterName=args["cluster_name"], addonName=addon_name)
                addon = ad["addon"]
                addon_health.append({
                    "name":    addon_name,
                    "version": addon.get("addonVersion"),
                    "status":  addon.get("status"),
                    "health":  addon.get("health", {}).get("issues", []),
                })
            except Exception:
                pass
        health_info["addon_health"] = addon_health
    except Exception as e:
        health_info["addon_health_error"] = str(e)

    # k8s-level checks if context provided
    ctx = args.get("context")
    if ctx:
        try:
            apis = _clients(ctx)
            nds = apis["core"].list_node().items
            not_ready = [
                n.metadata.name for n in nds
                if not any(
                    c.type == "Ready" and c.status == "True"
                    for c in (n.status.conditions or [])
                )
            ]
            health_info["nodes_total"]     = len(nds)
            health_info["nodes_not_ready"] = not_ready
        except Exception as e:
            health_info["node_check_error"] = str(e)

    return _ok(health_info)


async def _eks_list_nodegroups(args: dict) -> list[types.TextContent]:
    eks  = _boto3_eks(args)
    resp = eks.list_nodegroups(clusterName=args["cluster_name"])
    ngs  = resp.get("nodegroups", [])
    details = []
    for ng in ngs:
        try:
            d = eks.describe_nodegroup(clusterName=args["cluster_name"], nodegroupName=ng)
            ng_data = d["nodegroup"]
            details.append({
                "name":           ng,
                "status":         ng_data.get("status"),
                "instance_types": ng_data.get("instanceTypes"),
                "ami_type":       ng_data.get("amiType"),
                "scaling":        ng_data.get("scalingConfig"),
                "disk_size_gb":   ng_data.get("diskSize"),
                "release_version":ng_data.get("releaseVersion"),
            })
        except Exception as e:
            details.append({"name": ng, "error": str(e)})
    return _ok(details)


async def _eks_nodegroup_health(args: dict) -> list[types.TextContent]:
    eks  = _boto3_eks(args)
    cluster = args["cluster_name"]
    ng_name = args.get("nodegroup_name")

    if ng_name:
        nodegroups = [ng_name]
    else:
        resp = eks.list_nodegroups(clusterName=cluster)
        nodegroups = resp.get("nodegroups", [])

    result = []
    for ng in nodegroups:
        try:
            d  = eks.describe_nodegroup(clusterName=cluster, nodegroupName=ng)
            ng_data = d["nodegroup"]
            health_issues = ng_data.get("health", {}).get("issues", [])
            result.append({
                "nodegroup":      ng,
                "status":         ng_data.get("status"),
                "health_issues":  health_issues,
                "healthy":        len(health_issues) == 0,
                "instance_types": ng_data.get("instanceTypes"),
                "scaling": {
                    "desired": ng_data.get("scalingConfig", {}).get("desiredSize"),
                    "min":     ng_data.get("scalingConfig", {}).get("minSize"),
                    "max":     ng_data.get("scalingConfig", {}).get("maxSize"),
                },
                "release_version": ng_data.get("releaseVersion"),
                "ami_type":        ng_data.get("amiType"),
                "created_at":      str(ng_data.get("createdAt")),
            })
        except Exception as e:
            result.append({"nodegroup": ng, "error": str(e)})
    return _ok(result)


async def _eks_list_addons(args: dict) -> list[types.TextContent]:
    eks  = _boto3_eks(args)
    resp = eks.list_addons(clusterName=args["cluster_name"])
    addons = resp.get("addons", [])
    details = []
    for name in addons:
        try:
            d    = eks.describe_addon(clusterName=args["cluster_name"], addonName=name)
            addon = d["addon"]
            details.append({
                "name":           name,
                "version":        addon.get("addonVersion"),
                "status":         addon.get("status"),
                "health_issues":  addon.get("health", {}).get("issues", []),
                "service_account":addon.get("serviceAccountRoleArn"),
                "created_at":     str(addon.get("createdAt")),
                "modified_at":    str(addon.get("modifiedAt")),
            })
        except Exception as e:
            details.append({"name": name, "error": str(e)})
    return _ok(details)


async def _eks_update_kubeconfig(args: dict) -> list[types.TextContent]:
    cmd = [
        "aws", "eks", "update-kubeconfig",
        "--name", args["cluster_name"],
        "--region", args["region"],
        "--kubeconfig", KUBE_CONFIG_PATH,
    ]
    if args.get("profile"):
        cmd += ["--profile", args["profile"]]
    if args.get("alias"):
        cmd += ["--alias", args["alias"]]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return _ok((result.stdout + result.stderr).strip())


async def _eks_fargate_profiles(args: dict) -> list[types.TextContent]:
    eks  = _boto3_eks(args)
    resp = eks.list_fargate_profiles(clusterName=args["cluster_name"])
    profiles = resp.get("fargateProfileNames", [])
    details = []
    for pname in profiles:
        try:
            d = eks.describe_fargate_profile(
                clusterName=args["cluster_name"], fargateProfileName=pname
            )
            fp = d["fargateProfile"]
            details.append({
                "name":       pname,
                "status":     fp.get("status"),
                "selectors":  fp.get("selectors"),
                "pod_execution_role": fp.get("podExecutionRoleArn"),
                "subnets":    fp.get("subnets"),
                "created_at": str(fp.get("createdAt")),
            })
        except Exception as e:
            details.append({"name": pname, "error": str(e)})
    return _ok(details)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="kubernetes-mcp",
                server_version="1.0.0",
                capabilities=app.get_capabilities(
                    notification_options=None,
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
