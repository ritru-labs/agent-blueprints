"""CloudFormation parsing and retention proposals; never deploys stack changes."""

import copy
import json

import yaml

from .tools import AccessDenied


class TemplateLoader(yaml.SafeLoader):
    pass


def intrinsic(loader, suffix, node):
    if suffix not in {
        "Ref",
        "GetAtt",
        "Sub",
        "Join",
        "Select",
        "Split",
        "FindInMap",
        "ImportValue",
        "GetAZs",
        "If",
        "Equals",
        "And",
        "Or",
        "Not",
        "Base64",
    }:
        raise AccessDenied("Unsupported CloudFormation intrinsic")
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    return {suffix if suffix == "Ref" else "Fn::" + suffix: value}


TemplateLoader.add_multi_constructor("!", intrinsic)


def parse_template(text: str):
    if len(text.encode()) > 1_000_000:
        raise AccessDenied("Template exceeds size budget")
    # Reject aliases, anchors, and deep inputs before construction to bound expansion.
    depth = 0
    for event in yaml.parse(text):
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
            raise AccessDenied("Template aliases and anchors are not supported")
        if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
            depth += 1
        elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
            depth -= 1
        if depth > 50:
            raise AccessDenied("Template nesting exceeds budget")
    try:
        value = yaml.load(text, Loader=TemplateLoader)
    except yaml.YAMLError:
        raise AccessDenied("Invalid or unsupported CloudFormation template") from None
    if not isinstance(value, dict) or not isinstance(value.get("Resources"), dict):
        raise AccessDenied("CloudFormation Resources mapping required")
    if len(value["Resources"]) > 1000:
        raise AccessDenied("Template resource budget exceeded")
    # Ensure no dates, binary objects or recursive structures enter artifacts.
    json.dumps(value, allow_nan=False)
    return value


def retention_proposal(template: dict, logical_ids: tuple[str, ...]):
    if not logical_ids or len(set(logical_ids)) != len(logical_ids):
        raise AccessDenied("Select unique logical resources")
    result = copy.deepcopy(template)
    for identity in logical_ids:
        if identity not in result["Resources"]:
            raise AccessDenied("Unknown CloudFormation logical resource")
        resource = result["Resources"][identity]
        if resource.get("Type") not in {"AWS::EC2::VPC", "AWS::EC2::Subnet"}:
            raise AccessDenied("Source type has no transfer adapter")
        resource["DeletionPolicy"] = "Retain"
        resource["UpdateReplacePolicy"] = "Retain"
    return result


def release_proposal(template: dict, logical_ids: tuple[str, ...]):
    retained = retention_proposal(template, logical_ids)
    if retained != template:
        raise AccessDenied("Retention must already be deployed before ownership release")
    result = copy.deepcopy(template)
    for identity in logical_ids:
        del result["Resources"][identity]
    if not result["Resources"]:
        raise AccessDenied("Whole-stack removal requires a separate qualified procedure")
    if "Transform" in result:
        raise AccessDenied("Macros are not supported for ownership release")

    def check(value):
        if isinstance(value, dict):
            if value.get("Ref") in logical_ids:
                raise AccessDenied("Remaining template references a released resource")
            if "Fn::GetAtt" in value:
                reference = value["Fn::GetAtt"]
                name = reference.split(".")[0] if isinstance(reference, str) else reference[0]
                if name in logical_ids:
                    raise AccessDenied("Remaining template depends on a released resource")
            if "Fn::Sub" in value:
                raise AccessDenied("Substitution dependencies require a qualified resolver")
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)

    check(result)
    return result
