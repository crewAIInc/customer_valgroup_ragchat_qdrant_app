import os
import time
import json
import requests
import streamlit as st
import hmac
import boto3
import base64
from io import BytesIO
from botocore.exceptions import ClientError

# Set page configuration
st.set_page_config(page_title="CrewAI Document Processor", page_icon="🤖", layout="wide")

# Custom CSS for styling
st.markdown("""
<style>
.chat-role-user { color: #0068c9; font-weight: bold; display: inline; }
.chat-role-ai { color: #ff4b4b; font-weight: bold; display: inline; }
.chat-message { display: inline; margin-left: 8px; }
.small-api-status { font-size: 14px; margin-top: -15px; margin-bottom: 15px; }
.logo-container { position: relative; height: 60px; }
.logo-image { position: absolute; top: 0; left: 0; height: 40px; }
</style>
""", unsafe_allow_html=True)

# Authentication function
def check_password():
    """Returns `True` if the user had the correct password."""

    def password_entered():
        """Checks whether a password entered by the user is correct."""
        if hmac.compare_digest(st.session_state["username"], st.secrets["auth"]["username"]) and \
           hmac.compare_digest(st.session_state["password"], st.secrets["auth"]["password"]):
            st.session_state["password_correct"] = True
            # Delete password from session state
            del st.session_state["password"]
            del st.session_state["username"]
        else:
            st.session_state["password_correct"] = False
            st.error("😕 Username or password incorrect")

    # Return True if the password is validated
    if st.session_state.get("password_correct", False):
        return True

    # Show inputs for username + password
    st.title("CrewAI Use Case Generator Login")
    
    # Display login form
    with st.form("login_form"):
        st.text_input("Username", key="username")
        st.text_input("Password", type="password", key="password")
        st.form_submit_button("Login", on_click=password_entered)
    
    return False

# Check if the user is authenticated
if not check_password():
    st.stop()  # Stop execution if authentication failed

# If we get here, the user is authenticated
API_URL = st.secrets["CRW_API_URL"]
API_TOKEN = st.secrets["CRW_API_TOKEN"]

# AWS Credentials from secrets
AWS_ACCESS_KEY_ID = st.secrets["AWS_ACCESS_KEY_ID"]
AWS_SECRET_ACCESS_KEY = st.secrets["AWS_SECRET_ACCESS_KEY"]
AWS_DEFAULT_REGION = st.secrets["AWS_DEFAULT_REGION"]

# S3 URI path
S3_URI = st.secrets["S3_URI"]

# Initialize session state variables if they don't exist
if "processing" not in st.session_state:
    st.session_state.processing = False
if "kickoff_id" not in st.session_state:
    st.session_state.kickoff_id = None
if "upload_complete" not in st.session_state:
    st.session_state.upload_complete = False
if "chat_messages" not in st.session_state:
    st.session_state.chat_messages = []
if "chat_id" not in st.session_state:
    st.session_state.chat_id = None
if "should_clear_input" not in st.session_state:
    st.session_state.should_clear_input = False

# No duplicate import needed as base64 is already imported at the top

# Check API health function
def check_api_health():
    """Check if the CrewAI API is healthy using the status endpoint"""
    try:
        # First try checking status endpoint
        url = f"{API_URL}/status".rstrip("/")
        response = requests.get(url, headers={"Authorization": f"Bearer {API_TOKEN}"})
        
        # If status endpoint returns any response (even error), API is likely running
        if response.status_code < 500:
            return True
            
        # If status fails, try root endpoint as fallback
        root_response = requests.get(API_URL, headers={"Authorization": f"Bearer {API_TOKEN}"})
        return root_response.status_code < 500
    except:
        # If connection completely fails, API is not available
        return False

# Logo and title container
st.markdown("""
<div class="logo-container">
    <img src="data:image/svg+xml;base64,{}" class="logo-image">
</div>
""".format(base64.b64encode(open("crewai_logo.svg", "rb").read()).decode("utf-8")), unsafe_allow_html=True)

# Main app title
st.title("CrewAI Document Processor")

# API Status on top of main page (small)
status_col1, status_col2 = st.columns([1, 10])
with status_col1:
    st.markdown('<div class="small-api-status">API Status:</div>', unsafe_allow_html=True)
with status_col2:
    if check_api_health():
        st.markdown('<div class="small-api-status">✅ Connected</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="small-api-status">❌ Not available</div>', unsafe_allow_html=True)

# Function to make authenticated API requests
def api_request(endpoint, method="GET", data=None):
    """Make an authenticated request to the CrewAI API"""
    url = f"{API_URL}/{endpoint}".rstrip("/")
    headers = {
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type": "application/json"
    }
    
    try:
        if method == "GET":
            response = requests.get(url, headers=headers)
        elif method == "POST":
            response = requests.post(url, headers=headers, json=data)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
        
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        st.error(f"API request failed: {str(e)}")
        return None

# S3 utility functions
def parse_s3_uri(s3_uri):
    """Parse an S3 URI into bucket name and key."""
    parts = s3_uri.replace('s3://', '').split('/', 1)
    bucket = parts[0]
    prefix = parts[1] if len(parts) > 1 else ''
    return bucket, prefix

def get_s3_client():
    """Create and return an S3 client using credentials from secrets."""
    try:
        s3_client = boto3.client(
            's3',
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            region_name=AWS_DEFAULT_REGION
        )
        return s3_client
    except Exception as e:
        st.error(f"Error creating S3 client: {e}")
        raise

def list_s3_files(s3_folder_uri):
    """List all files in an S3 folder."""
    try:
        bucket, prefix = parse_s3_uri(s3_folder_uri)
        s3_client = get_s3_client()
        
        # Ensure the prefix ends with a slash if it's not empty
        if prefix and not prefix.endswith('/'):
            prefix += '/'
        
        # Use paginator for buckets with many objects
        paginator = s3_client.get_paginator('list_objects_v2')
        response_iterator = paginator.paginate(
            Bucket=bucket,
            Prefix=prefix
        )
        
        # Store file keys and S3 URIs
        files = []
        
        # Process each page of results
        for page in response_iterator:
            if 'Contents' in page:
                for obj in page['Contents']:
                    key = obj['Key']
                    # Skip the directory itself and directories
                    if key != prefix and not key.endswith('/'):
                        file_name = key.split('/')[-1]
                        file_uri = f"s3://{bucket}/{key}"
                        file_size = obj['Size']
                        # Format the size for display
                        if file_size < 1024:
                            size_str = f"{file_size} B"
                        elif file_size < 1024 * 1024:
                            size_str = f"{file_size / 1024:.1f} KB"
                        else:
                            size_str = f"{file_size / (1024 * 1024):.1f} MB"
                        
                        files.append({
                            "name": file_name, 
                            "uri": file_uri,
                            "size": file_size,
                            "size_str": size_str
                        })
        
        return files
    except Exception as e:
        st.error(f"Error listing files from S3: {e}")
        return []

def save_to_s3(file_content, file_name, s3_folder_uri):
    """Save a file to S3 bucket."""
    try:
        bucket, prefix = parse_s3_uri(s3_folder_uri)
        s3_client = get_s3_client()
        
        # Ensure the prefix ends with a slash if it's not empty
        if prefix and not prefix.endswith('/'):
            prefix += '/'
        
        # Create the full S3 key
        key = f"{prefix}{file_name}"
        
        # Upload the file
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=file_content.getvalue() if hasattr(file_content, 'getvalue') else file_content
        )
        
        return f"s3://{bucket}/{key}"
    except Exception as e:
        st.error(f"Error saving file to S3: {e}")
        return None

def save_uploaded_file(uploaded_file, s3_folder_uri):
    """Save an uploaded file to the specified S3 folder."""
    if uploaded_file is not None:
        file_uri = save_to_s3(uploaded_file, uploaded_file.name, s3_folder_uri)
        return file_uri
    return None

# Sidebar content - simplified
with st.sidebar:
    # Logout button
    st.subheader("Account")
    if st.button("Logout"):
        # Reset authentication status
        st.session_state["password_correct"] = False
        st.rerun()

# Create tabs for different sections
tab1, tab2, tab3 = st.tabs(["Upload Documents", "Process Documents", "Chat"])

with tab1:
    st.header("Upload Documents")
    
    # Upload interface
    st.subheader("Upload New Documents")
    st.markdown("Upload one or more documents to be processed by CrewAI.")
    uploaded_files = st.file_uploader("Choose files to upload", accept_multiple_files=True)
    
    # Upload button
    if uploaded_files:
        if st.button("Upload to S3"):
            with st.spinner("Uploading documents to S3..."):
                for uploaded_file in uploaded_files:
                    save_uploaded_file(uploaded_file, S3_URI)
                st.session_state.upload_complete = True
                st.success("Files successfully uploaded to S3!")
                st.rerun()
    
    # Display files in S3 bucket
    st.subheader("Documents in S3 Bucket")
    s3_files = list_s3_files(S3_URI)
    
    if s3_files:
        # Display files with details
        for file_info in s3_files:
            st.text(f"{file_info['name']} - {file_info['size_str']}")
    else:
        st.info("No documents found in S3 bucket.")
    
    # Upload tab instructions
    st.divider()
    st.subheader("Upload Documents Instructions")
    st.markdown("""
    1. Click the **Choose files to upload** button to select documents from your computer
    2. After selecting files, click the **Upload to S3** button to store them in the S3 bucket
    3. Once uploaded, documents will appear in the list above
    4. You can then proceed to the **Process Documents** tab to analyze the uploaded documents
    """)
    
    # Indicate that documents have been uploaded
    if uploaded_files:
        st.info("Documents uploaded successfully. You can process them in the 'Process Documents' tab.")

with tab2:
    st.header("Process Documents")
    
    # Process button that works on all documents in the S3 bucket
    st.subheader("Start Processing")
    st.markdown("Click the button below to process **all** documents in the S3 bucket.")
    
    # Process button not conditioned on uploads
    if st.button("Process All Documents", type="primary"):
        st.session_state.processing = True
        st.session_state.kickoff_id = None
        
        with st.spinner("Kicking off document processing..."):
            # Prepare input data with S3 URI
            input_data = {
                "inputs": {
                    "process_files": True,
                    "override_embeddings": False,
                }
            }
            
            # Make API request to kickoff processing
            kickoff_response = api_request("kickoff", method="POST", data=input_data)
            
            if kickoff_response and "kickoff_id" in kickoff_response:
                kickoff_id = kickoff_response["kickoff_id"]
                st.session_state.kickoff_id = kickoff_id
                st.success(f"Document processing kicked off successfully! Kickoff ID: {kickoff_id}")
            else:
                st.error("Failed to kickoff document processing")
                st.session_state.processing = False
    
    # Status section
    st.subheader("Process Status")
    
    # Status information
    if st.session_state.kickoff_id:
        st.success(f"Document processing is in progress. Kickoff ID: {st.session_state.kickoff_id}")
        
        # Create a placeholder for status updates
        status_container = st.empty()
        result_container = st.empty()
        
        # Refresh button
        if st.button("Refresh Status"):
            # Poll for status
            with st.spinner("Checking status..."):
                kickoff_id = st.session_state.kickoff_id
                status_data = api_request(f"status/{kickoff_id}")
                
                if status_data:
                    current_state = status_data.get('state', 'UNKNOWN')
                    status_container.info(f"Current State: {current_state}")
                    
                    # Check if execution is complete
                    if current_state == "SUCCESS":
                        result_container.success("DONE")
                        st.session_state.processing = False
                else:
                    status_container.error("Failed to retrieve status")
        
        # Auto-polling section
        if st.session_state.processing:
            with st.spinner("Waiting for processing to complete..."):
                # Poll for status until complete
                complete = False
                attempts = 0
                max_attempts = 30  # Limit polling to prevent infinite loops
                
                while not complete and attempts < max_attempts:
                    status_data = api_request(f"status/{st.session_state.kickoff_id}")
                    
                    if status_data:
                        current_state = status_data.get('state', 'UNKNOWN')
                        status_container.info(f"Current State: {current_state}")
                        
                        # Check if execution is complete
                        if current_state == "SUCCESS":
                            complete = True
                            result_container.success("DONE")
                            st.session_state.processing = False
                            break
                        
                        # If still running, wait and try again
                        time.sleep(0.1)
                    else:
                        status_container.error("Failed to retrieve status")
                        break
                    
                    attempts += 1
                
                # Handle execution timeout
                if attempts >= max_attempts and not complete:
                    status_container.warning("Processing is taking longer than expected.")
                    st.info(f"You can manually refresh the status to check progress.")
    else:
        st.info("No processing has been started yet. Click 'Process All Documents' above to start.")
    
    # Process Documents tab instructions
    st.divider()
    st.subheader("Process Documents Instructions")
    st.markdown("""
    1. Click the **Process All Documents** button to start processing all documents in the S3 bucket
    2. Processing status will be displayed in the **Process Status** section above
    3. You can click **Refresh Status** to check the current progress
    4. When processing is complete, you'll see **DONE** displayed
    5. Once complete, go to the **Chat** tab to ask questions about your processed documents
    """)

# Chat tab
with tab3:
    st.header("Chat with Documents")
    
    # Display chat history
    chat_container = st.container()
    with chat_container:
        for message in st.session_state.chat_messages:
            if message["role"] == "user":
                st.markdown(f"<div class='chat-role-user'>You:</div><div class='chat-message'>{message['content']}</div>", unsafe_allow_html=True)
            else:
                st.markdown(f"<div class='chat-role-ai'>AI:</div><div class='chat-message'>{message['content']}</div>", unsafe_allow_html=True)
    
    # Input for user message with more chatty UI
    # Create the text input
    key = f"user_message_{len(st.session_state.chat_messages)}"
    user_input = st.text_input("  ", key=key, on_change=lambda: setattr(st.session_state, "_submit_clicked", True) if st.session_state.get(key) else None)
    col1, col2 = st.columns([5, 1])
    with col2:
        submit_clicked = st.button("Send")
        if submit_clicked:
            st.session_state["_submit_clicked"] = True
    
    # Process message when Send is clicked or Enter is pressed
    if submit_clicked or (user_input and st.session_state.get("_submit_clicked", False)):
        # Store the current message for processing
        current_message = user_input
        # Reset the submit flag
        st.session_state["_submit_clicked"] = False
        
        # Add user message to chat history
        st.session_state.chat_messages.append({"role": "user", "content": current_message})
        
        # Prepare API request data
        if st.session_state.chat_id is None:
            # Initial message
            input_data = {
                "inputs": {
                    "current_message": current_message
                }
            }
        else:
            # Follow-up message
            input_data = {
                "inputs": {
                    "current_message": current_message,
                    "id": st.session_state.chat_id
                }
            }
        
        # Two-step API request process
        with st.spinner("Getting response..."):
            # Step 1: Kickoff the request to get kickoff_id
            kickoff_response = api_request("kickoff", method="POST", data=input_data)
            
            if kickoff_response and "kickoff_id" in kickoff_response:
                # Get the kickoff_id from the response
                kickoff_id = kickoff_response["kickoff_id"]
                
                # Step 2: Check status with the kickoff_id
                # Add a small delay to allow processing
                time.sleep(0.1)
                
                # Poll the status endpoint until we get a result
                max_retries = 120
                for i in range(max_retries):
                    status_response = api_request(f"status/{kickoff_id}", method="GET")
                    
                    if status_response and status_response.get("state") == "SUCCESS" and status_response.get("result"):
                        # Parse the result which is a JSON string
                        try:
                            result_str = status_response["result"]
                            result_data = json.loads(result_str)
                            
                            # Extract the actual response and chat ID
                            response_text = result_data.get("response", "")
                            chat_id = result_data.get("id")
                            
                            # Update the chat_id for future messages
                            if chat_id:
                                st.session_state.chat_id = chat_id
                            
                            # Add AI response to chat history
                            st.session_state.chat_messages.append({"role": "assistant", "content": response_text})
                            break
                        except json.JSONDecodeError as e:
                            st.error(f"Error parsing result JSON: {str(e)}")
                            break
                    elif status_response and status_response.get("state") in ["FAILURE", "TIMEOUT"]:
                        st.error(f"Request failed with state: {status_response.get('state')}")
                        break
                    elif i == max_retries - 1:
                        st.error("Timed out waiting for response")
                    else:
                        # Wait before trying again
                        time.sleep(0.1)
            else:
                st.error("Failed to get a kickoff ID from the API")
                if kickoff_response:
                    st.error(f"Received: {kickoff_response}")
            
            # No need to set a flag anymore as we're using a new key each time
            # Rerun to update the UI with the new messages
            st.rerun()
    
    # Clear chat button
    if st.button("Clear Chat"):
        st.session_state.chat_messages = []
        st.session_state.chat_id = None
        st.rerun()
