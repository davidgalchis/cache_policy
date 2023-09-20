import boto3
import botocore
# import jsonschema
import json
import traceback
import zipfile
import os
import subprocess
import logging

from botocore.exceptions import ClientError

from extutil import remove_none_attributes, account_context, ExtensionHandler, ext, \
    current_epoch_time_usec_num, component_safe_name, lambda_env, random_id, \
    handle_common_errors

eh = ExtensionHandler()

# Import your clients
client = boto3.client('cloudfront')

logger = logging.getLogger()
logger.setLevel(logging.INFO)

"""
eh calls
    eh.add_op() call MUST be made for the function to execute! Adds functions to the execution queue.
    eh.add_props() is used to add useful bits of information that can be used by this component or other components to integrate with this.
    eh.add_links() is used to add useful links to the console, the deployed infrastructure, the logs, etc that pertain to this component.
    eh.retry_error(a_unique_id_for_the_error(if you don't want it to fail out after 6 tries), progress=65, callback_sec=8)
        This is how to wait and try again
        Only set callback seconds for a wait, not an error
        @ext() runs the function if its operation is present and there isn't already a retry declared
    eh.add_log() is how logs are passed to the front-end
    eh.perm_error() is how you permanently fail the component deployment and send a useful message to the front-end
    eh.finish() just finishes the deployment and sends back message and progress
    *RARE*
    eh.add_state() takes a dictionary, merges existing with new
        This is specifically if CloudKommand doesn't need to store it for later. Thrown away at the end of the deployment.
        Preserved across retries, but not across deployments.
There are three elements of state preserved across retries:
    - eh.props
    - eh.links 
    - eh.state 
Wrap all operations you want to run with the following:
    @ext(handler=eh, op="your_operation_name")
Progress only needs to be explicitly reported on 1) a retry 2) an error. Finishing auto-sets progress to 100. 
"""

def safe_cast(val, to_type, default=None):
    try:
        return to_type(val)
    except (ValueError, TypeError):
        return default
    

def lambda_handler(event, context):
    try:
        # All relevant data is generally in the event, excepting the region and account number
        print(f"event = {event}")
        region = account_context(context)['region']
        account_number = account_context(context)['number']

        # This copies the operations, props, links, retry data, and remaining operations that are sent from CloudKommand. 
        # Just always include this.
        eh.capture_event(event)

        # These are other important values you will almost always use
        prev_state = event.get("prev_state") or {}
        project_code = event.get("project_code")
        repo_id = event.get("repo_id")
        cdef = event.get("component_def")
        cname = event.get("component_name")

        # you pull in whatever arguments you care about
        """
        # Some examples. S3 doesn't really need any because each attribute is a separate call. 
        auto_verified_attributes = cdef.get("auto_verified_attributes") or ["email"]
        alias_attributes = cdef.get("alias_attributes") or ["preferred_username", "phone_number", "email"]
        username_attributes = cdef.get("username_attributes") or None
        """

        name = prev_state.get("props", {}).get("name") or cdef.get("name") or component_safe_name(project_code, repo_id, cname, no_underscores=True, no_uppercase=True, max_chars=63)


        ### ATTRIBUTES THAT CAN BE SET ON INITIAL CREATION
        description = cdef.get("description")
        default_ttl = cdef.get("default_ttl") or 86400
        max_ttl = cdef.get("max_ttl") or 31536000
        min_ttl = cdef.get("min_ttl")
        enable_accept_encoded_gzip = cdef.get("enable_accept_encoded_gzip") or True
        enable_accept_encoded_brotli = cdef.get("enable_accept_encoded_brotli") or True
        header_behavior = cdef.get("header_behavior") or "none"
        headers = cdef.get("headers") or [] # This must be a list of strings. The plugin must calculate the quantity of items and add that as as quantity flag
        cookie_behavior = cdef.get("cookie_behavior") or "none"
        cookies = cdef.get("cookies") or [] # This must be a list of strings. The plugin must calculate the quantity of items and add that as as quantity flag
        query_string_behavior = cdef.get("query_string_behavior") or "none"
        query_strings = cdef.get("query_strings") or [] # This must be a list of strings. The plugin must calculate the quantity of items and add that as as quantity flag

        # remove any None values from the attributes dictionary        
        attributes = remove_none_attributes({
            'Comment': str(description) if description else description,
            'Name': str(name) if name else name,
            'DefaultTTL': int(default_ttl) if default_ttl else default_ttl,
            'MaxTTL': int(max_ttl) if max_ttl else max_ttl,
            'MinTTL': int(min_ttl) if min_ttl else min_ttl,
            'ParametersInCacheKeyAndForwardedToOrigin': {
                'EnableAcceptEncodingGzip': enable_accept_encoded_gzip,
                'EnableAcceptEncodingBrotli': enable_accept_encoded_brotli,
                'HeadersConfig': remove_none_attributes({
                    'HeaderBehavior': str(header_behavior) if header_behavior else header_behavior,
                    'Headers': {
                        'Quantity': len(headers),
                        'Items': headers
                    } if headers else None
                }),
                'CookiesConfig': remove_none_attributes({
                    'CookieBehavior': str(cookie_behavior) if cookie_behavior else cookie_behavior,
                    'Cookies': {
                        'Quantity': len(cookies),
                        'Items': cookies
                    } if cookies else None
                }),
                'QueryStringsConfig': remove_none_attributes({
                    'QueryStringBehavior': str(query_string_behavior) if query_string_behavior else query_string_behavior,
                    'QueryStrings': {
                        'Quantity': len(query_strings),
                        'Items': query_strings
                    } if query_strings else None
                })
            }
        }) 

        ### DECLARE STARTING POINT
        pass_back_data = event.get("pass_back_data", {}) # pass_back_data only exists if this is a RETRY
        # If a RETRY, then don't set starting point
        if pass_back_data:
            pass # If pass_back_data exists, then eh has already loaded in all relevant RETRY information.
        # If NOT retrying, and we are instead upserting, then we start with the GET STATE call
        elif event.get("op") == "upsert":

            eh.add_op("get_cache_policy")

        # If NOT retrying, and we are instead deleting, then we start with the DELETE call 
        #   (sometimes you start with GET STATE if you need to make a call for the identifier)
        elif event.get("op") == "delete":
            eh.add_op("delete_cache_policy")
            eh.add_state({"cache_policy_id": prev_state["props"]["id"]})

        # The ordering of call declarations should generally be in the following order
        # GET STATE
        # CREATE
        # UPDATE
        # DELETE
        # GENERATE PROPS
        
        ### The section below DECLARES all of the calls that can be made. 
        ### The eh.add_op() function MUST be called for actual execution of any of the functions. 

        ### GET STATE
        get_cache_policy(attributes, region, prev_state)

        ### DELETE CALL(S)
        delete_cache_policy()

        ### CREATE CALL(S) (occasionally multiple)
        create_cache_policy(attributes, region, prev_state)
        
        ### UPDATE CALLS (common to have multiple)
        # You want ONE function per boto3 update call, so that retries come back to the EXACT same spot. 
        update_cache_policy(attributes, region, prev_state)

        ### GENERATE PROPS (sometimes can be done in get/create)

        # IMPORTANT! ALWAYS include this. Sends back appropriate data to CloudKommand.
        return eh.finish()

    # You want this. Leave it.
    except Exception as e:
        msg = traceback.format_exc()
        print(msg)
        eh.add_log("Unexpected Error", {"error": msg}, is_error=True)
        eh.declare_return(200, 0, error_code=str(e))
        return eh.finish()

### GET STATE
# ALWAYS put the ext decorator on ALL calls that are referenced above
# This is ONLY called when this operation is slated to occur.
# GENERALLY, this function will make a bunch of eh.add_op() calls which determine what actions will be executed.
#   The eh.add_op() call MUST be made for the function to execute!
# eh.add_props() is used to add useful bits of information that can be used by this component or other components to integrate with this.
# eh.add_links() is used to add useful links to the console, the deployed infrastructure, the logs, etc that pertain to this component.
@ext(handler=eh, op="get_cache_policy")
def get_cache_policy(attributes, region, prev_state):
    
    existing_cache_policy_id = prev_state.get("props", {}).get("id")

    if existing_cache_policy_id:
        # Try to get the cache_policy. If you succeed, record the props and links from the current cache_policy
        try:
            response = client.get_cache_policy(
                Id=existing_cache_policy_id
            )
            cache_policy_to_use = None
            if response and response.get("CachePolicy"):
                eh.add_log("Got Cache Policy", response)
                cache_policy_to_use = response.get("CachePolicy")
                cache_policy_id = cache_policy_to_use.get("Id")
                cache_policy_config = cache_policy_to_use.get("CachePolicyConfig")
                eh.add_state({"cache_policy_id": cache_policy_id, "region": region})
                existing_props = {
                    "id": cache_policy_id,
                    "name": cache_policy_config.get("Name"),
                    "description": cache_policy_config.get("Comment"),
                    "etag": response.get("ETag")
                }
                eh.add_props(existing_props)
                eh.add_links({"Rule": gen_cache_policy_link(region, cache_policy_id=cache_policy_id)})

                ### If the listener exists, then setup any followup tasks
                populated_existing_attributes = remove_none_attributes(existing_props)
                current_attributes = remove_none_attributes({
                    "ListenerArn": str(populated_existing_attributes.get("listener_arn")) if populated_existing_attributes.get("listener_arn") else populated_existing_attributes.get("listener_arn"),
                    "Priority": int(populated_existing_attributes.get("priority")) if populated_existing_attributes.get("priority") else str(populated_existing_attributes.get("priority")),
                    "Conditions": conditions_to_formatted_conditions(reformatted_conditions) if reformatted_conditions else None,
                    "Actions": [{
                        "Type": str(populated_existing_attributes.get("action_type")) if populated_existing_attributes.get("action_type") else populated_existing_attributes.get("action_type"),
                        "TargetGroupArn": str(populated_existing_attributes.get("target_group_arn")) if populated_existing_attributes.get("target_group_arn") else populated_existing_attributes.get("target_group_arn"),
                    }]
                })

                comparable_attributes = {i:attributes[i] for i in attributes if i not in ['Tags', 'Priority']}
                comparable_current_attributes = {i:current_attributes[i] for i in current_attributes if i not in ['Tags', 'Priority']}
                if comparable_attributes != comparable_current_attributes:
                    eh.add_op("update_cache_policy")

                if attributes.get("Priority") != current_attributes.get("Priority"):
                    eh.add_op("update_cache_policy_priority")

                try:
                    # Try to get the current tags
                    response = client.describe_tags(ResourceArns=[cache_policy_id])
                    eh.add_log("Got Tags")
                    relevant_items = [item for item in response.get("TagDescriptions") if item.get("ResourceArn") == cache_policy_id]
                    current_tags = {}

                    # Parse out the current tags
                    if len(relevant_items) > 0:
                        relevant_item = relevant_items[0]
                        if relevant_item.get("Tags"):
                            current_tags = key_value_list_obj_to_compressed_dict(relevant_item.get("Tags"))
                            # {item.get("Key") : item.get("Value") for item in relevant_item.get("Tags")}

                    # If there are tags specified, figure out which ones need to be added and which ones need to be removed
                    if attributes.get("Tags"):

                        tags = attributes.get("Tags")
                        formatted_tags = key_value_list_obj_to_compressed_dict(tags)
                        # {item.get("Key") : item.get("Value") for item in tags}
                        # Compare the current tags to the desired tags
                        if formatted_tags != current_tags:
                            remove_tags = [k for k in current_tags.keys() if k not in formatted_tags]
                            add_tags = {k:v for k,v in formatted_tags.items() if v != current_tags.get(k)}
                            if remove_tags:
                                eh.add_op("remove_tags", remove_tags)
                            if add_tags:
                                eh.add_op("set_tags", add_tags)
                    # If there are no tags specified, make sure to remove any straggler tags
                    else:
                        if current_tags:
                            eh.add_op("remove_tags", list(current_tags.keys()))

                # If the load balancer does not exist, some wrong has happened. Probably don't permanently fail though, try to continue.
                except client.exceptions.RuleNotFoundException:
                    eh.add_log("Listener Rule Not Found", {"arn": cache_policy_id})
                    pass

            else:
                eh.add_log("Listener Rule Does Not Exist", {"listener_arn": existing_cache_policy_id})
                eh.add_op("create_cache_policy")
                return 0
        # If there is no listener and there is an exception handle it here
        except client.exceptions.RuleNotFoundException:
            eh.add_log("Listener Rule Does Not Exist", {"cache_policy_id": existing_cache_policy_id})
            eh.add_op("create_cache_policy")
            return 0
        except ClientError as e:
            print(str(e))
            eh.add_log("Get Listener Rule Error", {"error": str(e)}, is_error=True)
            eh.retry_error("Get Listener Rule Error", 10)
            return 0
    else:
        eh.add_log("Listener Rule Does Not Exist", {"cache_policy_id": existing_cache_policy_id})
        eh.add_op("create_cache_policy")
        return 0

            
@ext(handler=eh, op="create_cache_policy")
def create_cache_policy(attributes, region, prev_state):

    try:
        response = client.create_cache_policy(**attributes)
        cache_policy = response.get("Rules")[0]
        cache_policy_id = cache_policy.get("RuleArn")

        eh.add_log("Created Listener Rule", cache_policy)
        eh.add_state({"cache_policy_id": cache_policy.get("RuleArn"), "region": region})
        props_to_add = {
            "arn": cache_policy.get("RuleArn"),
            "listener_arn": attributes.get("ListenerArn"),
            "conditions": formatted_conditions_to_conditions(cache_policy.get("Conditions")),
            "priority": cache_policy.get("Priority"),
            "action_type": cache_policy.get("Actions", [{}])[0].get("Type"),
            "target_group_arn": cache_policy.get("Actions", [{}])[0].get("TargetGroupArn"),
        }
        eh.add_props(props_to_add)
        eh.add_links({"Rule": gen_cache_policy_link(region, cache_policy_id=cache_policy.get("RuleArn"))})

        ### Once the listener cache_policy exists, then setup any followup tasks

        try:
            # Try to get the current tags
            response = client.describe_tags(ResourceArns=[cache_policy_id])
            eh.add_log("Got Tags")
            relevant_items = [item for item in response.get("TagDescriptions") if item.get("ResourceArn") == cache_policy_id]
            current_tags = {}

            # Parse out the current tags
            if len(relevant_items) > 0:
                relevant_item = relevant_items[0]
                if relevant_item.get("Tags"):
                    current_tags = key_value_list_obj_to_compressed_dict(relevant_item.get("Tags"))
                    # {item.get("Key") : item.get("Value") for item in relevant_item.get("Tags")}

            # If there are tags specified, figure out which ones need to be added and which ones need to be removed
            if attributes.get("Tags"):

                tags = attributes.get("Tags")
                formatted_tags = key_value_list_obj_to_compressed_dict(tags)
                # {item.get("Key") : item.get("Value") for item in tags}
                # Compare the current tags to the desired tags
                if formatted_tags != current_tags:
                    remove_tags = [k for k in current_tags.keys() if k not in formatted_tags]
                    add_tags = {k:v for k,v in formatted_tags.items() if v != current_tags.get(k)}
                    if remove_tags:
                        eh.add_op("remove_tags", remove_tags)
                    if add_tags:
                        eh.add_op("set_tags", add_tags)
            # If there are no tags specified, make sure to remove any straggler tags
            else:
                if current_tags:
                    eh.add_op("remove_tags", list(current_tags.keys()))

        # If the load balancer does not exist, some wrong has happened. Probably don't permanently fail though, try to continue.
        except client.exceptions.RuleNotFoundException:
            eh.add_log("Rule Not Found", {"arn": cache_policy_id})
            pass

    except client.exceptions.PriorityInUseException as e:
        eh.add_log(f"Priority {attributes.get('Priority')} assigned to this listener cache_policy already exists", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyTargetGroupsException as e:
        eh.add_log(f"AWS Quota for Target Groups reached. Please increase your quota and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyRulesException as e:
        eh.add_log(f"AWS Quota for Rules per Listener reached. Please increase your quota and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TargetGroupAssociationLimitException as e:
        eh.add_log(f"Too many Target Groups associated with the Rule. Please decrease the number of Target Groups associated to this Listener Rule and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.ListenerNotFoundException as e:
        eh.add_log(f"Listener provided for this Listener Rule was not found.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TargetGroupNotFoundException as e:
        eh.add_log(f"Target Group provided for this Listener Rule was not found.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.InvalidConfigurationRequestException as e:
        eh.add_log(f"The configuration provided for this Listener Rule is invalid. Please enter valid configuration and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyRegistrationsForTargetIdException as e:
        eh.add_log(f"Too many registrations for the Target provided. Please decrease the number of registrations for the Target and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyTargetsException as e:
        eh.add_log(f"Too many Targets provided for this Listener Rule. Please decrease the number of Targets for the Listener Rule and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyActionsException as e:
        eh.add_log(f"Too many Actions provided for this Listener Rule. Please decrease the number of Actions for the Listener Rule and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.InvalidLoadBalancerActionException as e:
        eh.add_log(f"Load Balancer action specified is invalid. Please change the Load Balancer action specified and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyUniqueTargetGroupsPerLoadBalancerException as e:
        eh.add_log(f"AWS Quota for Unique Target Groups per Load Balancer reached. Please increase your quota and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyTagsException as e:
        eh.add_log("Too Many Tags on Listener Rule. You may have 50 tags per resource.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except ClientError as e:
        handle_common_errors(e, eh, "Error Creating Listener Rule", progress=20)


@ext(handler=eh, op="remove_tags")
def remove_tags():

    remove_tags = eh.ops.get('remove_tags')
    cache_policy_id = eh.state["cache_policy_id"]

    try:
        response = client.remove_tags(
            ResourceArns=[cache_policy_id],
            TagKeys=remove_tags
        )
        eh.add_log("Removed Tags", remove_tags)
    except client.exceptions.ListenerNotFoundException:
        eh.add_log("Listener Rule Not Found", {"arn": cache_policy_id})

    except ClientError as e:
        handle_common_errors(e, eh, "Error Removing Listener Rule Tags", progress=90)


@ext(handler=eh, op="set_tags")
def set_tags():

    tags = eh.ops.get("set_tags")
    cache_policy_id = eh.state["cache_policy_id"]
    try:
        response = client.add_tags(
            ResourceArns=[cache_policy_id],
            Tags=[{"Key": key, "Value": value} for key, value in tags.items()]
        )
        eh.add_log("Tags Added", response)

    except client.exceptions.RuleNotFoundException as e:
        eh.add_log("Listener Rule Not Found", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 90)
    except client.exceptions.DuplicateTagKeysException as e:
        eh.add_log(f"Duplicate Tags Found. Please remove duplicates and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 90)
    except client.exceptions.TooManyTagsException as e:
        eh.add_log(f"Too Many Tags on Listener Rule. You may have 50 tags per resource.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 90)

    except ClientError as e:
        handle_common_errors(e, eh, "Error Adding Tags", progress=90)


@ext(handler=eh, op="update_cache_policy")
def update_cache_policy(attributes, region, prev_state):
    modifiable_attributes = {i:attributes[i] for i in attributes if i not in ['Tags', "ListenerArn", "Priority"]}
    modifiable_attributes["RuleArn"] = eh.state["cache_policy_id"]
    try:
        response = client.modify_cache_policy(**modifiable_attributes)
        cache_policy = response.get("Rules")[0]
        cache_policy_id = cache_policy.get("RuleArn")
        eh.add_log("Updated Listener Rule", cache_policy)
        existing_props = {
            "arn": cache_policy_id,
            "listener_arn": attributes.get("ListenerArn"),
            "conditions": formatted_conditions_to_conditions(cache_policy.get("Conditions")),
            "priority": cache_policy.get("Priority"),
            "action_type": cache_policy.get("Actions", [{}])[0].get("Type"),
            "target_group_arn": cache_policy.get("Actions", [{}])[0].get("TargetGroupArn"),
        }
        eh.add_props(existing_props)
        eh.add_links({"Rule": gen_cache_policy_link(region, cache_policy_id=cache_policy_id)})

    except client.exceptions.TargetGroupAssociationLimitException as e:
        eh.add_log(f"Too many Target Groups associated with the Rule. Please decrease the number of Target Groups associated to this Listener Rule and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.RuleNotFoundException as e:
        eh.add_log(f"Listener Rule Not Found", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.RuleNotFoundException as e:
        eh.add_log(f"The Operation specified is not permitted", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyRegistrationsForTargetIdException as e:
        eh.add_log(f"Too many registrations for the Target provided. Please decrease the number of registrations for the Target and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyTargetsException as e:
        eh.add_log(f"Too many Targets provided for this Listener Rule. Please decrease the number of Targets for the Listener Rule and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TargetGroupNotFoundException as e:
        eh.add_log(f"Target Group provided for this Listener Rule was not found.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyActionsException as e:
        eh.add_log(f"Too many Actions provided for this Listener Rule. Please decrease the number of Actions for the Listener Rule and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.InvalidLoadBalancerActionException as e:
        eh.add_log(f"Load Balancer action specified is invalid. Please change the Load Balancer action specified and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyUniqueTargetGroupsPerLoadBalancerException as e:
        eh.add_log(f"AWS Quota for Unique Target Groups per Load Balancer reached. Please increase your quota and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except ClientError as e:
        handle_common_errors(e, eh, "Error Creating Listener Rule", progress=20)

@ext(handler=eh, op="update_cache_policy_priority")
def update_cache_policy_priority(priority):

    cache_policy_id = eh.state["cache_policy_id"]
    try:
        response = client.set_cache_policy_priorities(
            RulePriorities=[
                {
                    'RuleArn': cache_policy_id,
                    'Priority': int(priority) if priority else priority
                },
            ]
        )
        eh.add_props({"Priority": priority})
        eh.add_log(f"Updated Listener Rule Priority")
    except client.exceptions.RuleNotFoundException:
        eh.add_log("Listener Rule Not Found", {"cache_policy_id": cache_policy_id})
        eh.perm_error(str(e), 30)
    except client.exceptions.PriorityInUseException as e:
        eh.add_log(f"Priority {priority} assigned to this listener cache_policy already exists", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 30)
    except client.exceptions.OperationNotPermittedException as e:
        eh.add_log(f"The operation of updating the priority on this listener cache_policy is not permitted.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 30)
    except ClientError as e:
        handle_common_errors(e, eh, "Error Updating the Listener Rule Priority", progress=80)


@ext(handler=eh, op="delete_cache_policy")
def delete_cache_policy():
    cache_policy_id = eh.state["cache_policy_id"]
    try:
        response = client.delete_cache_policy(
            RuleArn=cache_policy_id
        )
        eh.add_log("Listener Rule Deleted", {"cache_policy_id": cache_policy_id})
    except client.exceptions.RuleNotFoundException as e:
        eh.add_log(f"Listener Rule Not Found", {"error": str(e)}, is_error=True)
        return 0
    except ClientError as e:
        handle_common_errors(e, eh, "Error Deleting Listener Rule", progress=80)
    

def gen_cache_policy_link(region, cache_policy_id):
    return f"https://{region}.console.aws.amazon.com/ec2/home?region={region}#ListenerRuleDetails:cache_policyArn={cache_policy_id}"


