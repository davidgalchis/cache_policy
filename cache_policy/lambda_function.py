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
        description = cdef.get("description") or f"CloudKommand Managed Cache Policy {name}"
        default_ttl = cdef.get("default_ttl") or 86400
        max_ttl = cdef.get("max_ttl") or 31536000
        min_ttl = cdef.get("min_ttl") or 1
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
            payload = {
                "Id": existing_cache_policy_id
            }
            response = client.get_cache_policy(**payload)
            cache_policy_to_use = None
            if response and response.get("CachePolicy"):
                eh.add_log("Got Cache Policy", response)
                cache_policy_to_use = response.get("CachePolicy")
                cache_policy_id = cache_policy_to_use.get("Id")
                cache_policy_etag = response.get("ETag")
                cache_policy_config = cache_policy_to_use.get("CachePolicyConfig")
                eh.add_state({"cache_policy_id": cache_policy_id, "etag": cache_policy_etag, "region": region})
                existing_props = {
                    "id": cache_policy_id,
                    "name": cache_policy_config.get("Name"),
                    "description": cache_policy_config.get("Comment"),
                    "etag": cache_policy_etag
                }
                eh.add_props(existing_props)
                eh.add_links({"Cache Policy": gen_cache_policy_link(region, cache_policy_id=cache_policy_id)})

                ### If the cache policy exists, then setup any followup tasks
                if attributes != cache_policy_config:
                    eh.add_op("update_cache_policy")

            else:
                eh.add_log("Cache Policy Does Not Exist", {"cache_policy_id": existing_cache_policy_id})
                eh.add_op("create_cache_policy")
                return 0
        # If there is no cache policy and there is an exception handle it here
        except client.exceptions.NoSuchCachePolicy:
            eh.add_log("Cache Policy Does Not Exist", {"cache_policy_id": existing_cache_policy_id})
            eh.add_op("create_cache_policy")
            return 0
        except client.exceptions.AccessDenied: # I believe this should not happen unless the plugin has insufficient permissions
            eh.add_log("You do you not have access to the specified cache policy. Please update the permissions and try again.", {"cache_policy_id": existing_cache_policy_id})
            eh.add_op("create_cache_policy")
            return 0
        except ClientError as e:
            print(str(e))
            eh.add_log("Get Cache Policy Error", {"error": str(e)}, is_error=True)
            eh.retry_error("Get Cache Policy Error", 10)
            return 0
    else:
        eh.add_log("Cache Policy Does Not Exist", {"cache_policy_id": existing_cache_policy_id})
        eh.add_op("create_cache_policy")
        return 0

            
@ext(handler=eh, op="create_cache_policy")
def create_cache_policy(attributes, region, prev_state):

    try:
        response = client.create_cache_policy(
            CachePolicyConfig=attributes
        )
        cache_policy = response.get("CachePolicy")
        cache_policy_id = cache_policy.get("Id")
        cache_policy_config = cache_policy.get("CachePolicyConfig")

        eh.add_log("Created Cache Policy", cache_policy)
        eh.add_state({"cache_policy_id": cache_policy_id, "etag": cache_policy_etag, "region": region})
        props_to_add = {
            "id": cache_policy_id,
            "name": cache_policy_config.get("Name"),
            "description": cache_policy_config.get("Comment"),
            "etag": response.get("ETag")
        }
        eh.add_props(props_to_add)
        eh.add_links({"Cache Policy": gen_cache_policy_link(region, cache_policy_id=cache_policy_id)})

        ### Once the cache_policy exists, then setup any followup tasks

        # N/A, in the case of this plugin

    except client.exceptions.AccessDenied as e:
        eh.add_log(f"You do you not have the required permissions to create the specified cache policy. Please update permissions and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.CachePolicyAlreadyExists as e:
        eh.add_log(f"The cache policy already exists. Please edit your component definition and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyCachePolicies as e:
        eh.add_log(f"AWS Quota for Cache Policies reached. Please increase your quota and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyHeadersInCachePolicy as e:
        eh.add_log(f"Too many headers have been added to the Cache Policy. Please decrease the number of headers and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyCookiesInCachePolicy as e:
        eh.add_log(f"Too many cookies have been added to the Cache Policy. Please decrease the number of cookies and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyQueryStringsInCachePolicy as e:
        eh.add_log(f"Too many query strings have been added to the Cache Policy. Please decrease the number of query strings and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.InvalidArgument as e:
        eh.add_log("An invalid argument has been passed to the create cache policy call. Please check the arguments specified and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.InconsistentQuantities as e:
        eh.add_log("The number for the quantity of list items in either headers, cookies, or query strings does not match the amount passed. Please make sure each value specified is a valid value.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except ClientError as e:
        handle_common_errors(e, eh, "Error Creating Cache Policy", progress=20)


@ext(handler=eh, op="update_cache_policy")
def update_cache_policy(attributes, region, prev_state):
    cache_policy_id = eh.state["cache_policy_id"]
    cache_policy_etag = eh.state["cache_policy_etag"]
    try:
        response = client.update_cache_policy(
            Id=cache_policy_id,
            IfMatch=cache_policy_etag,
            CachePolicyConfig=attributes
        )
        cache_policy = response.get("CachePolicy")
        cache_policy_id = cache_policy.get("Id")
        cache_policy_config = cache_policy.get("CachePolicyConfig")
        eh.add_log("Updated Cache Policy", cache_policy)
        existing_props = {
            "id": cache_policy_id,
            "name": cache_policy_config.get("Name"),
            "description": cache_policy_config.get("Comment"),
            "etag": response.get("ETag")
        }
        eh.add_props(existing_props)
        eh.add_links({"Cache Policy": gen_cache_policy_link(region, cache_policy_id=cache_policy_id)})




    except client.exceptions.AccessDenied as e:
        eh.add_log(f"You do you not have the required permissions to update the specified cache policy. Please update permissions and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.IllegalUpdate as e:
        eh.add_log(f"The update attempted contains modifications that are not allowed.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.InvalidIfMatchVersion as e:
        eh.add_log(f"The \"If-Match\" version is missing or not valid.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.NoSuchCachePolicy as e:
        eh.add_log(f"Cache Policy Does Not Exist", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.PreconditionFailed as e:
        eh.add_log(f"The precondition for one or more of the request fields was not met.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.CachePolicyAlreadyExists as e:
        eh.add_log(f"The cache policy already exists. Please edit your component definition and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyCachePolicies as e:
        eh.add_log(f"AWS Quota for Cache Policies reached. Please increase your quota and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyHeadersInCachePolicy as e:
        eh.add_log(f"Too many headers have been added to the Cache Policy. Please decrease the number of headers and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyCookiesInCachePolicy as e:
        eh.add_log(f"Too many cookies have been added to the Cache Policy. Please decrease the number of cookies and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyQueryStringsInCachePolicy as e:
        eh.add_log(f"Too many query strings have been added to the Cache Policy. Please decrease the number of query strings and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.InvalidArgument as e:
        eh.add_log("An invalid argument has been passed to the update cache policy call. Please check the arguments specified and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.InconsistentQuantities as e:
        eh.add_log("The number for the quantity of list items in either headers, cookies, or query strings does not match the amount passed. Please make sure each value specified is a valid value.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except ClientError as e:
        handle_common_errors(e, eh, "Error Creating Cache Policy", progress=20)


@ext(handler=eh, op="delete_cache_policy")
def delete_cache_policy():
    cache_policy_id = eh.state["cache_policy_id"]
    cache_policy_etag = eh.state["cache_policy_etag"]
    try:
        response = client.delete_cache_policy(
            Id=cache_policy_id,
            IfMatch=cache_policy_etag
        )
        eh.add_log("Cache Policy Deleted", {"cache_policy_id": cache_policy_id, "cache_policy_etag": cache_policy_etag})

    except client.exceptions.AccessDenied as e:
        eh.add_log(f"You do you not have the required permissions to delete the specified cache policy. Please update permissions and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.InvalidIfMatchVersion as e:
        eh.add_log(f"The \"If-Match\" version is missing or not valid.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.NoSuchCachePolicy as e:
        eh.add_log(f"Cache Policy Not Found", {"error": str(e)}, is_error=True)
        return 0
    except client.exceptions.PreconditionFailed as e:
        eh.add_log(f"The precondition for one or more of the request fields was not met.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.IllegalDelete as e:
        eh.add_log(f"You cannot delete a managed policy.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.CachePolicyInUse as e:
        eh.add_log(f"Cannot delete the cache policy because it is attached to one or more cache behaviors.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except ClientError as e:
        handle_common_errors(e, eh, "Error Deleting Cache Policy", progress=80)
    

def gen_cache_policy_link(region, cache_policy_id):
    return f"https://{region}.console.aws.amazon.com/cloudfront/v3/home?region={region}#/policies/cache/{cache_policy_id}"


