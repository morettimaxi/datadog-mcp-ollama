import os
import json
import subprocess
import time
import re
import ollama
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configuration
MCP_SERVER_COMMAND = ['node', 'mcp-server-datadog/build/index.js']
OLLAMA_MODEL = 'mistral:latest'  # Or choose another model you have pulled

# Available tools definition
AVAILABLE_TOOLS = [
    {
        "name": "get_monitors",
        "description": "Fetch the status of Datadog monitors.",
        "parameters": {
            "type": "object",
            "properties": {
                "groupStates": {"type": "array", "items": {"type": "string"}, "description": "States to filter (e.g., alert, warn, no data, ok)."},
                "name": {"type": "string", "description": "Filter by name."},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Filter by tags."}
            },
            "required": []
        }
    },
    {
        "name": "list_incidents",
        "description": "Retrieve a list of incidents from Datadog.",
        "parameters": {
            "type": "object",
            "properties": {
                "filter": {"type": "string", "description": "Filter parameters for incidents (e.g., status, priority)."},
                "pagination": {"type": "object", "description": "Pagination details like page size/offset."}
            },
            "required": []
        }
    },
    {
        "name": "list_dashboards",
        "description": "Get a list of dashboards from Datadog.",
        "parameters": {
            "type": "object",
            "properties": {
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Filter dashboards by tags."}
            },
            "required": []
        }
    }
    # Add other tools as needed
]

# System prompt for Ollama
SYSTEM_PROMPT = f"""You are a Datadog SRE assistant that helps users retrieve information from Datadog.
You have access to these tools:
{json.dumps(AVAILABLE_TOOLS, indent=2)}

When the user asks for Datadog information, you should:
1. Determine which tool to use
2. Generate a JSON tool call to execute the appropriate command
3. Keep it simple and focused on the task

Your JSON tool call should be in this format:
```json
{{
    "tool_name": "get_monitors",
    "arguments": {{}}
}}
```

IMPORTANT RULES:
1. NEVER include comments in JSON - NO COMMENTS LIKE // or /* */ ANYWHERE in the JSON, not even as examples
2. If the user doesn't specify optional parameters, OMIT them entirely
3. Only include parameters the user has explicitly mentioned
4. Always respond to direct questions or requests for Datadog data with a tool call
5. DO NOT invent fake data or make up responses
6. ALWAYS format your JSON properly with DOUBLE QUOTES for all keys and string values
7. DO NOT include explanations inside the JSON, put them before or after

SPECIAL INSTRUCTIONS FOR ALERT MONITORS:
- If a user asks for "alert" monitors, use "get_monitors" with groupStates parameter set to ["alert"]
- When filtering monitors by status, use the groupStates parameter, not the status field
- For example: {{"tool_name": "get_monitors", "arguments": {{"groupStates": ["alert"]}}}}
"""

def call_mcp_tool(tool_name, arguments=None):
    """Calls the mcp-server-datadog tool via subprocess."""
    if arguments is None:
        arguments = {}
    
    # If requesting monitors in alert state, set the groupStates parameter correctly
    if tool_name == "get_monitors" and "groupStates" not in arguments:
        # Always check for all group states if requesting monitors
        arguments["groupStates"] = ["alert", "warn", "no data", "ok"]
    
    request_id = f"req-{int(time.time()*1000)}"
    mcp_request = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments
        }
    }
    request_json = json.dumps(mcp_request) + '\n'
    
    print(f"\n--- Calling MCP Tool: {tool_name} ---")
    print(f"Arguments: {json.dumps(arguments, indent=2)}")

    try:
        process = subprocess.Popen(
            MCP_SERVER_COMMAND,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            env=os.environ.copy()
        )

        stdout_data, stderr_data = process.communicate(input=request_json, timeout=15)

        if process.returncode != 0:
            print(f"Error: MCP server process failed with exit code {process.returncode}: {stderr_data}")
            return {"error": f"MCP server process failed with exit code {process.returncode}: {stderr_data}"}

        if not stdout_data.strip():
            print("Error: Received empty response from MCP server.")
            return {"error": "Received empty response from MCP server."}

        # Print the raw stdout for debugging
        print(f"--- Raw MCP Output ---\n{stdout_data}")

        # Parse the response
        try:
            response = None
            for line in stdout_data.strip().split('\n'):
                try:
                    parsed = json.loads(line)
                    if parsed.get('id') == request_id:
                        response = parsed
                        break
                except:
                    continue
            
            if response is None:
                return {"error": "Could not find valid JSON-RPC response in MCP output."}
            
            if 'result' in response:
                # Extract the actual data
                try:
                    raw_text_result = response['result']['content'][0]['text']
                    print(f"--- Raw Tool Result Text ---\n{raw_text_result}")
                    
                    # Try to extract JSON if present
                    json_part = raw_text_result
                    if ': [' in raw_text_result:
                        start_index = raw_text_result.find('[')
                        if start_index != -1:
                            json_part = raw_text_result[start_index:]
                    elif ': {' in raw_text_result:
                        start_index = raw_text_result.find('{')
                        if start_index != -1:
                            json_part = raw_text_result[start_index:]
                    
                    try:
                        parsed_result = json.loads(json_part)
                        # If this is a monitor list, check for alert state monitors specifically
                        if tool_name == "get_monitors" and isinstance(parsed_result, list) and "status:alert" in arguments.get("filter", "").lower():
                            # Filter to just alert state monitors
                            alert_monitors = [monitor for monitor in parsed_result if monitor.get("status", "").upper() == "ALERT"]
                            print(f"--- Filtered {len(alert_monitors)} alert monitors from {len(parsed_result)} total ---")
                            return {"result": alert_monitors}
                        return {"result": parsed_result}
                    except json.JSONDecodeError as e:
                        print(f"Error parsing JSON from response: {e}")
                        # Return the raw text if parsing failed
                        return {"result": raw_text_result}
                        
                except (KeyError, IndexError, TypeError) as e:
                    print(f"Error extracting content: {e}")
                    return {"error": f"Could not extract content from MCP response: {e}", "raw_response": response}
            elif 'error' in response:
                return {"error": f"MCP error: {response['error']}"}
            else:
                return {"error": "Invalid MCP response format.", "raw_response": response}
                
        except json.JSONDecodeError as e:
            return {"error": f"Failed to parse MCP response as JSON: {e}", "raw_stdout": stdout_data}

    except Exception as e:
        print(f"Error: {str(e)}")
        return {"error": f"Error calling MCP tool: {str(e)}"}

def process_user_input(user_message, chat_history=None):
    """Process user input, detect tool calls via Ollama, and execute them."""
    if chat_history is None:
        chat_history = []
    
    # Detect if this is a request specifically for alerts
    is_alert_request = False
    if re.search(r'(get|list|show).*\b(alert|alerts|alerting)\b', user_message.lower()):
        is_alert_request = True
        print("--- Detected request for alert monitors ---")
    
    # Prepare messages for Ollama
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT}
    ]
    messages.extend(chat_history)
    messages.append({"role": "user", "content": user_message})
    
    # Call Ollama
    print("--- Sending message to Ollama ---")
    response = ollama.chat(model=OLLAMA_MODEL, messages=messages)
    ollama_response = response['message']['content']
    print(f"--- Ollama Response ---\n{ollama_response}")
    
    # Try to extract a tool call from the response
    tool_call = None
    try:
        # Extract JSON content, but clean it first to remove any potential comments
        json_blocks = re.findall(r'```(?:json)?\s*(.*?)\s*```', ollama_response, re.DOTALL)
        if json_blocks:
            # Take the first JSON block
            json_content = json_blocks[0]
        else:
            # Try to find standalone JSON
            json_match = re.search(r'(\{.*\})', ollama_response, re.DOTALL)
            if json_match:
                json_content = json_match.group(1)
            else:
                json_content = None
        
        if json_content:
            # Clean potential comments before parsing
            # Remove single-line comments (// ...)
            clean_json = re.sub(r'//.*?($|\n)', '', json_content)
            # Remove multi-line comments (/* ... */)
            clean_json = re.sub(r'/\*.*?\*/', '', clean_json, flags=re.DOTALL)
            
            parsed_json = json.loads(clean_json)
            if isinstance(parsed_json, dict) and 'tool_name' in parsed_json:
                tool_call = parsed_json
                print(f"--- Detected Tool Call ---\n{json.dumps(tool_call, indent=2)}")
                
                # Ensure proper parameters for alert requests
                if is_alert_request and tool_call.get('tool_name') == 'get_monitors':
                    args = tool_call.get('arguments', {})
                    if 'groupStates' not in args:
                        args['groupStates'] = ["alert"]
                        tool_call['arguments'] = args
                        print("--- Added groupStates: [alert] to tool call ---")
    except Exception as e:
        print(f"--- Failed to extract tool call: {e} ---")
        print(f"--- Attempted to parse: {json_content if 'json_content' in locals() else 'No JSON content found'} ---")
    
    # If a tool call was detected, execute it
    if tool_call:
        tool_name = tool_call.get('tool_name')
        arguments = tool_call.get('arguments', {})
        
        # Clean arguments (remove None values)
        cleaned_args = {k: v for k, v in arguments.items() if v is not None}
        
        # Call the MCP tool
        tool_result = call_mcp_tool(tool_name, cleaned_args)
        
        # Update chat history
        chat_history.append({"role": "assistant", "content": ollama_response})
        chat_history.append({"role": "tool", "content": json.dumps(tool_result)})
        
        # Send the tool result back to Ollama
        messages.append({"role": "assistant", "content": ollama_response})
        messages.append({"role": "tool", "content": json.dumps(tool_result)})
        
        # Get final response
        print("--- Sending tool result back to Ollama ---")
        final_response = ollama.chat(model=OLLAMA_MODEL, messages=messages)
        final_content = final_response['message']['content']
        chat_history.append({"role": "assistant", "content": final_content})
        
        return final_content, chat_history
    else:
        # No tool call detected, just return Ollama's response
        chat_history.append({"role": "assistant", "content": ollama_response})
        return ollama_response, chat_history

def main():
    """Main function to run the console-based assistant."""
    print("💻 Datadog SRE Console Assistant")
    print("Type 'exit' or 'quit' to end the conversation.")
    print("Ask questions like 'get monitors' or 'list incidents in alert state'.")
    print("-" * 50)
    
    chat_history = []
    
    while True:
        user_input = input("\n> ")
        if user_input.lower() in ['exit', 'quit']:
            print("Goodbye!")
            break
        
        try:
            response, chat_history = process_user_input(user_input, chat_history)
            print("\n" + response)
        except Exception as e:
            print(f"\nError: {str(e)}")

if __name__ == "__main__":
    main() 