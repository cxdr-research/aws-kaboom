"""Python code for identifying risks using a Graph generated by Principal Mapper. The findings are tracked using
dictionary objects with the format:

{
   "title": <str>,
   "severity": "Low|Medium|High",
   "impact": <str>,
   "description": <str>,
   "recommendation": <str>
}
"""


#  Copyright (c) NCC Group and Erik Steringer 2019. This file is part of Principal Mapper.
#
#      Principal Mapper is free software: you can redistribute it and/or modify
#      it under the terms of the GNU Affero General Public License as published by
#      the Free Software Foundation, either version 3 of the License, or
#      (at your option) any later version.
#
#      Principal Mapper is distributed in the hope that it will be useful,
#      but WITHOUT ANY WARRANTY; without even the implied warranty of
#      MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#      GNU Affero General Public License for more details.
#
#      You should have received a copy of the GNU Affero General Public License
#      along with Principal Mapper.  If not, see <https://www.gnu.org/licenses/>.

import datetime as dt
import json
from typing import List, Optional, Tuple

import principalmapper
from principalmapper.analysis.finding import Finding
from principalmapper.analysis.report import Report
from principalmapper.common import Graph, Node, Edge
from principalmapper.querying import query_interface
from principalmapper.querying.local_policy_simulation import resource_policy_authorization, ResourcePolicyEvalResult
from principalmapper.querying.presets.privesc import can_privesc
from principalmapper.util import arns


def gen_findings_and_print(graph: Graph, formatting: str) -> None:
    """Generates findings of risk, prints them out."""

    report = gen_report(graph)

    if formatting == 'text':
        print_report(report)
    else:  # format == 'json'
        print(json.dumps(report.as_dictionary(), indent=4))


def gen_report(graph: Graph) -> Report:
    """Generates a Report object with findings and metadata about report-generation"""
    findings = gen_all_findings(graph)
    return Report(
        graph.metadata['account_id'],
        dt.datetime.now(dt.timezone.utc),
        findings,
        'Findings identified using Principal Mapper ({}) from NCC Group: https://github.com/nccgroup/PMapper'.format(
            principalmapper.__version__
        )
    )


def gen_all_findings(graph: Graph) -> List[Finding]:
    """Generates findings of risk, returns a list of finding-dictionary objects."""
    result = []
    result.extend(gen_privesc_findings(graph))
    result.extend(gen_admin_users_without_mfa_finding(graph))
    result.extend(gen_mfa_actions_findings(graph))
    # TODO: result.extend(gen_mfa_evasion_finding(graph))  # policies that allow attackers to change MFA devices
    result.extend(gen_overprivileged_function_findings(graph))
    result.extend(gen_overprivileged_instance_profile_findings(graph))
    result.extend(gen_overprivileged_stack_findings(graph))
    result.extend(gen_os_lpe_finding(graph))  # policies on EC2 instances that allow LPE with ssm-agent
    result.extend(gen_circular_access_finding(graph))  # nodes that can circularly access each other
    return result


def gen_privesc_findings(graph: Graph) -> List[Finding]:
    """Generates findings related to privilege escalation risks."""
    result = []

    node_path_list = []

    for node in graph.nodes:
        if node.is_admin:
            continue  # skip current admins
        privesc_res, edge_list = can_privesc(graph, node)
        if privesc_res:
            node_path_list.append((node, edge_list))

    if len(node_path_list) > 0:
        description_preamble = 'In AWS, IAM Principals such as IAM Users or IAM Roles have their permissions defined ' \
                               'using IAM Policies. These policies describe different actions, resources, and ' \
                               'conditions where the principal can make a given API call to a service.\n\n' \
                               'Administrative principals can call any action with any resource, as in the ' \
                               'AdministratorAccess AWS-managed policy. However, some permissions may allow another ' \
                               'non-administrative principal to gain access to an administrative principal. ' \
                               'This represents a privilege escalation risk.\n\n' \
                               'The following principals could escalate privileges:\n\n'

        description_body = ''
        for node, edge_list in node_path_list:
            end_of_list = edge_list[-1].destination
            description_body += '* {} can escalate privileges by accessing the administrative principal {}:\n'.format(
                node.searchable_name(), end_of_list.searchable_name())
            for edge in edge_list:
                description_body += '   * {}\n'.format(edge.describe_edge())
            description_body += '\n'

        result.append(Finding(
            'IAM {} Can Escalate Privileges'.format('Principals' if len(node_path_list) > 1 else 'Principal'),
            'High',
            'A lower-privilege IAM User or Role is able to gain administrative privileges. This could lead to the '
            'lower-privilege principal being used to compromise the account and its resources.',
            description_preamble + description_body,
            'Review the IAM Policies that are applicable to the affected IAM User(s) or Role(s). Either reduce the '
            'permissions of the administrative principal(s), or reduce the permissions of the principal(s) that can '
            'access the administrative principals.'
        ))

    return result


def gen_mfa_actions_findings(graph: Graph) -> List[Finding]:
    """Generates findings related to risk from IAM Users able to call sensitive actions without needing MFA."""
    result = []
    affected_users = []
    for node in graph.nodes:
        if ':user/' in node.arn and node.is_admin and node.access_keys > 0:
            # Check if the given admin user with access keys can call sensitive actions without MFA
            # TODO: Check for other actions in here?
            actions = ['iam:CreateUser', 'iam:CreateRole', 'iam:CreateGroup', 'iam:PutUserPolicy', 'iam:PutRolePolicy',
                       'iam:PutGroupPolicy', 'iam:AttachUserPolicy', 'iam:AttachRolePolicy', 'iam:AttachGroupPolicy',
                       'sts:AssumeRole']
            if _can_call_without_mfa(node, actions):
                affected_users.append(node)

    if len(affected_users) > 0:
        description_preamble = 'In AWS, IAM Users can be configured to use an MFA device. When an IAM User has MFA ' \
                               'enabled, they are required to provide the second factor of authentication when they ' \
                               'log in to the AWS Console. However, unless there is a specific IAM policy attached ' \
                               'to the user, they will not need to provide a second factor of authentication when ' \
                               'making API calls.\n\nThe following administrative IAM Users have at least one set of ' \
                               'access keys, and can call sensitive actions to alter permissions or add users ' \
                               'without using a second factor of authentication:\n\n'

        description_body = ''
        for node in affected_users:
            description_body += '* {}\n'.format(node.searchable_name())

        result.append(Finding(
            'Administrative IAM {} Can Call Sensitive Actions Without MFA'.format(
                'Users' if len(affected_users) > 1 else 'User'
            ),
            'Medium',
            'An adminstrative IAM User is able to call sensitive actions, such as creating more principals or '
            'modifying permissions, without using MFA.',
            description_preamble + description_body,
            'Implement and attach an IAM Policy to the noted user(s) that rejects requests when MFA is not used.'
        ))

    return result


def _can_call_without_mfa(node: Node, actions: List[str]) -> bool:
    """Returns true if node can call sensitive action without MFA"""
    for action in actions:
        auth, needmfa = query_interface.local_check_authorization_handling_mfa(
            node,
            action,
            '*',
            {}
        )
        if auth and not needmfa:
            return True
    return False


def gen_overprivileged_instance_profile_findings(graph: Graph) -> List[Finding]:
    """Generates findings related to risk from EC2 instances being loaded with overprivileged instance profiles."""
    result = []
    affected_roles = []
    for node in graph.nodes:
        if ':role/' in node.arn and node.is_admin and len(node.instance_profile) > 0:
            affected_roles.append(node)

    if len(affected_roles) > 0:
        description_preamble = 'In AWS, EC2 instances can be given an instance profile. These instance profiles ' \
                               'are associated with an IAM Role, and grants access to the permissions of the IAM ' \
                               'Role. Because EC2 instances are at a higher risk of exposure and compromise, both ' \
                               'to external attackers and authorized users in the AWS account, they should not have ' \
                               'access to administrative privileges. The following IAM Roles have administrative ' \
                               'permissions and are associated with an instance profile:\n\n'

        description_body = ''
        for node in affected_roles:
            description_body += '* {}\n'.format(node.searchable_name())

        result.append(Finding(
            'Instance {} Administrator Privileges'.format(
                'Profiles Have' if len(affected_roles) > 1 else 'Profile Has'
            ),
            'High',
            'If an instance with the noted instance profile(s) is compromised, then the AWS account as a whole is at '
            'risk of compromise.',
            description_preamble + description_body,
            'Reduce the scope of permissions attached to the noted instance profile(s).'
        ))

    return result


def gen_overprivileged_function_findings(graph: Graph) -> List[Finding]:
    """Generates findings related to risk from Lambda functions being loaded with overprivileged roles"""
    result = []
    affected_roles = []
    for node in graph.nodes:
        if ':role/' in node.arn and node.is_admin:
            if query_interface.resource_policy_authorization('lambda.amazonaws.com', arns.get_account_id(node.arn),
                                                             node.trust_policy, 'sts:AssumeRole', node.arn, {})\
                    == query_interface.ResourcePolicyEvalResult.SERVICE_MATCH:
                affected_roles.append(node)

    if len(affected_roles) > 0:
        description_preamble = 'In AWS, Lambda functions can be assigned an IAM Role to use during execution. These ' \
                               'IAM Roles give the function access to call the AWS API with the permissions of the ' \
                               'IAM Role, depending on the policies attached to it. If the Lambda function can be ' \
                               'compromised, and the attacker can alter the code it executes, the attacker could ' \
                               'make AWS API calls with the IAM Role\'s permissions. The following IAM Roles have ' \
                               'administrative privileges, and can be passed to Lambda functions:\n\n'

        description_body = ''
        for node in affected_roles:
            description_body += '* {}\n'.format(node.searchable_name())

        result.append(Finding(
            'IAM Roles Available to Lambda Functions Have Administrative Privileges' if len(affected_roles) > 1 else
            'IAM Role Available to Lambda Functions Has Administrative Privileges',
            'Medium',
            'If an attacker can inject code or commands into the function, or if a lower-privileged principal can '
            'alter the function, the AWS account as a whole could be compromised.',
            description_preamble + description_body,
            'Reduce the scope of permissions attached to the noted IAM Role(s).'
        ))

    return result


def gen_overprivileged_stack_findings(graph: Graph) -> List[Finding]:
    """Generates findings related to risk from CloudFormation stacks being loaded with overprivileged roles"""
    result = []
    affected_roles = []
    for node in graph.nodes:
        if ':role/' in node.arn and node.is_admin:
            if query_interface.resource_policy_authorization('cloudformation.amazonaws.com',
                                                             arns.get_account_id(node.arn), node.trust_policy,
                                                             'sts:AssumeRole', node.arn, {}) == \
                    query_interface.ResourcePolicyEvalResult.SERVICE_MATCH:
                affected_roles.append(node)

    if len(affected_roles) > 0:
        description_preamble = 'In AWS, CloudFormation stacks can be given an IAM Role. When a stack has an IAM ' \
                               'Role, it can use that IAM Role to make AWS API calls to create the resources ' \
                               'defined in the template for that stack. If the IAM Role has administrator access ' \
                               'to the account, and an attacker is able to make the right CloudFormation API calls, ' \
                               'they would be able to use the IAM Role to escalate privileges and compromise the ' \
                               'account as a whole. The following IAM Roles can be used in CloudFormation and ' \
                               'have administrative privileges:\n\n'

        description_body = ''
        for node in affected_roles:
            description_body += '* {}\n'.format(node.searchable_name())

        result.append(Finding(
            'IAM Roles Available to CloudFormation Stacks Have Administrative Privileges' if len(affected_roles) > 1
            else 'IAM Role Available to CloudFormation Stacks Has Administrative Privileges',
            'Low',
            'If an attacker has the right permissions in the AWS Account, they can grant themselves adminstrative '
            'access to the account to compromise the account.',
            description_preamble + description_body,
            'Reduce the scope of permissions attached to the noted IAM Role(s).'
        ))

    return result


def gen_os_lpe_finding(graph: Graph) -> List[Finding]:
    """Generates findings related to risk of SSM permissions being misconfigured (local priv-esc on the host)"""
    result = []
    affected_roles = []
    for node in graph.nodes:
        if ':role/' in node.arn and node.instance_profile is not None and len(node.instance_profile) > 0:
            # https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-setting-up-messageAPIs.html
            if query_interface.local_check_authorization(node, 'ssmmessages:*', '*', {}):
                if query_interface.local_check_authorization(node, 'ssm:SendCommand', '*', {}):
                    affected_roles.append(node)
                elif query_interface.local_check_authorization(node, 'ssm:StartSession', '*', {}):
                    affected_roles.append(node)

    if len(affected_roles) > 0:
        description_preamble = 'In AWS EC2, instances can be assigned instance profiles. An instance profile is tied ' \
                               'to a single IAM Role. The instance profile can be used to access the AWS API with ' \
                               'the permissions of the IAM Role. If the IAM Role has permission to call certain SSM ' \
                               'actions such as `ssm:SendCommand` or `ssm:StartSession`, the instance profile ' \
                               'can be used to invoke commands on other instances or itself.' \
                               '\n' \
                               '\n' \
                               'Because the SSM Agent runs with the highest permissions on their hosts ' \
                               '(root or SYSTEM), this can be a way for attackers to pivot and compromise other ' \
                               'instances, or escalate privileges on the local machine. The following IAM Roles ' \
                               'are attached to at least one instance profile and have permissions with the ' \
                               'aforementioned risk:' \
                               '\n' \
                               '\n'

        description_body = ''
        for node in affected_roles:
            description_body += '* {}\n'.format(node.searchable_name())

        result.append(Finding(
            'IAM Roles With Unsafe SSM Permissions' if len(affected_roles) > 1
            else 'IAM Role With Unsafe SSM Permissions',
            'Medium',
            'If an attacker gains access to an instance with the unsafe permissions, they could escalate privileges '
            'on its current host or compromise other hosts.',
            description_preamble + description_body,
            'Reduce the scope of permissions attached to the noted IAM Role(s).'
        ))

    return result


def _find_cycle(graph: Graph, origin: Node) -> Optional[List[Node]]:
    """Internal method for finding a cycle with a given node. Does a Depth-first Search to attempt to identify one."""

    current_root = origin
    current_stack = [origin]
    explored_nodes = []

    while len(current_stack) > 0:
        outbound_nodes = [x.destination for x in current_root.get_outbound_edges(graph)]
        if len(outbound_nodes) == 0:
            current_root = current_stack.pop()
        else:
            for node in outbound_nodes:
                if node == origin:
                    return current_stack
            candidates = [x for x in outbound_nodes if x not in explored_nodes]
            if len(candidates) == 0:
                current_root = current_stack.pop()
                continue
            else:
                explored_nodes.append(current_root)
                current_root = candidates[0]
                current_stack.append(current_root)

    return None


def gen_circular_access_finding(graph: Graph) -> List[Finding]:
    """Generates findings related to the risk of a set of nodes that can circularly access each other.
    Admins excluded."""

    result = []
    cycles = []

    for node in graph.nodes:
        if node.is_admin:
            continue

        cycle_result = _find_cycle(graph, node)
        if cycle_result is not None:
            cycles.append(cycle_result)

    if len(cycles) > 0:
        description_preamble = 'In AWS, an IAM Principal with a specific set of permissions can gain access ' \
                               'to another principal, such as when an IAM User has permission to call ' \
                               '`sts:AssumeRole` for an IAM Role. Principal Mapper tracks these connections as ' \
                               'Nodes (a.k.a. Vertices) and Edges in a Graph.' \
                               '\n' \
                               '\n' \
                               'However, there may be instances where nodes can access each other in a circular ' \
                               'manner. This presents a risk in the account if an attacker gains access to one ' \
                               'of the principals in a cycle. An attacker can abuse that access to pivot between ' \
                               'each of the principals in a cycle. This can be used to evade detection or ' \
                               'persist access to an AWS account. The following cycles were identified:' \
                               '\n' \
                               '\n'

        description_body = ''
        for cycle in cycles:
            description_body += '* {}\n'.format(' -> '.join([x.searchable_name() for x in cycle] + [cycle[0].searchable_name()]))

        result.append(Finding(
            'IAM Principals with Circular Access',
            'Low',
            'If an attacker gains access to one of the identified principals, they can potentially evade detections '
            'or persist access.',
            description_preamble + description_body,
            'Break the cycle of access by altering/removing permissions assigned to one of the noted principals.'
        ))

    return result


def gen_admin_users_without_mfa_finding(graph: Graph) -> List[Finding]:
    """Generates findings related to IAM Users that have administrative privileges in an AWS account but no
    MFA device configured."""

    result = []
    affected_nodes = []

    for node in graph.nodes:
        if node.searchable_name().startswith('user/') and node.is_admin and not node.has_mfa:
            affected_nodes.append(node)

    if len(affected_nodes) > 0:
        description_preamble = 'In AWS, an IAM User can be assigned a device for Multi-Factor Authentication (MFA). ' \
                               'When an IAM User is assigned an MFA device, they are required to provide an extra ' \
                               'factor of authentication when logging in to the AWS Console. It is also possible to ' \
                               'create IAM Policies that impose extra restrictions on the permissions of IAM Users ' \
                               'depending on whether or not they have authenticated with MFA when using the AWS API. ' \
                               'Any IAM User with administrative privileges should be configured to have an MFA ' \
                               'device. \n\n' \
                               'The following IAM Users with administrative privileges do not have an MFA ' \
                               'device configured:' \
                               '\n' \
                               '\n'

        user_list = []
        for node in affected_nodes:
            user_list.append('* {}'.format(node.searchable_name()))
        description_body = '\n'.join(user_list)

        result.append(Finding(
            'IAM Users With Administrative Permissions But No MFA Device',
            'Medium',
            'If an attacker gains access to any of the noted sensitive IAM Users, there is no secondary layer of '
            'protection in place to prevent the AWS from being compromised.',
            description_preamble + description_body,
            'Assign an MFA device to each of the noted IAM Users.'
        ))

    return result


def gen_resources_with_potential_confused_deputies(graph: Graph) -> List[Finding]:
    """Generates findings related to AWS resources that allow access to AWS services (via resource policy)
    that may not correctly verify which AWS account is the true source of a request that
    affects the given resource.

    Primarily works by inspecting resource policies and making sure that access is guarded
    with a condition using aws:SourceAccount."""

    result = []

    resource_service_action_map = {
        's3': {
            'serverlessrepo.amazonaws.com': [
                's3:GetObject'
            ]
        }
    }

    affected_policies = []  # type: List[Tuple[str, str, str]]
    for resource_type in resource_service_action_map.keys():
        for policy in graph.policies:
            if arns.get_service(policy.arn) == resource_type:
                for service, action_list in resource_service_action_map[resource_type].items():
                    available_actions = []
                    for action in action_list:
                        rpa_result = resource_policy_authorization(
                            service,
                            graph.metadata['account_id'],
                            policy.policy_doc,
                            action,
                            policy.arn,
                            {
                                'aws:SourceAccount': '000000000000'
                            }
                        )
                        if rpa_result.SERVICE_MATCH:
                            available_actions.append(action)
                    if len(available_actions) > 0:
                        affected_policies.append(
                            (policy.arn, service, ' | '.join(available_actions))
                        )

    if len(affected_policies) > 0:
        desc_list_str = '\n'.join(['* With service {}, the resource {} for the action(s): {}'.format(y, x, z) for x, y, z in affected_policies])
        result.append(
            Finding(
                'Resources With A Potential Confused-Deputy Risk',
                'Medium',
                'Depending on the affected resources and services, an attacker may be able to execute read or write '
                'operations on the resources from another AWS account.',
                'In AWS, certain services will create and use resources in the customer\'s own AWS account. This may '
                'be controlled using a resource policy that grants access to the service that created the resource '
                'in the customer\'s AWS account. However, some services require customers to use the '
                '`${aws:SourceAccount}` condition context key to control access to the account resource from the '
                'service. In other words, to prevent the service from accessing the resource on the behalf of '
                'another customer, the resource needs a resource policy that allow-lists the true "source" of a '
                'request.\n\n'
                'The following AWS services and resources could allow an external account to potentially gain '
                'read/write access to the resources:\n\n' + desc_list_str,
                'Update the resource policy for all affected resources, and ensure that all statements granting '
                'access to AWS services check against the `${aws:SourceAccount}` condition context key when '
                'appropriate.'
            )
        )

    return result


def print_report(report: Report) -> None:
    """Given a report, uses print() to print out their contents in a Markdown format."""

    # Preamble
    print('----------------------------------------------------------------')
    print('# Principal Mapper Findings')
    print()
    print('Findings identified in AWS account {}'.format(report.account))
    print()
    print('Date and Time: {}'.format(report.date_and_time.isoformat()))
    print()
    print(report.source)
    print()

    # Findings
    if len(report.findings) == 0:
        print("None found.")
        print()
    else:
        for finding in report.findings:
            print(
                "## {}\n\n### Severity\n\n{}\n\n### Impact\n\n{}\n\n### Description\n\n{}\n\n### Recommendation\n\n{}\n\n".format(
                    finding.title, finding.severity, finding.impact, finding.description,finding.recommendation
                )
            )

    # Footer

    print()
    print('----------------------------------------------------------------')
