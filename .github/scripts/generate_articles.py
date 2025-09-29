import base64
import os
import json
import time
import requests
import markdown
import mimetypes
import yaml
import re
import smtplib
import random
import csv
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
from datetime import datetime, timedelta
from PIL import Image
from google import genai
from google.genai import types
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import nltk
from nltk.corpus import stopwords
from nltk.tokenize import word_tokenize

# Configure output directory
OUTPUT_DIR = "generated-articles"
KEYWORDS_FILE = "data/keywords.txt"
PROCESSED_KEYWORDS_FILE = "data/processed_keywords.txt"
GENERATED_KEYWORDS_FILE = "data/keywords-generated.txt"  # New file for successfully generated keywords
LINKS_FILE = "data/links.txt"  # New file for tracking links
ARTICLES_PER_RUN = 2
TOP_LINKS_COUNT = 20  # Number of top relevant links to include

# Ensure output directories exist
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(os.path.dirname(PROCESSED_KEYWORDS_FILE), exist_ok=True)
os.makedirs(os.path.dirname(GENERATED_KEYWORDS_FILE), exist_ok=True)
os.makedirs(os.path.dirname(LINKS_FILE), exist_ok=True)

# Download NLTK data if needed
try:
    nltk.data.find('tokenizers/punkt')
    nltk.data.find('corpora/stopwords')
except LookupError:
    nltk.download('punkt')
    nltk.download('stopwords')

class APIKeyManager:
    """Manages multiple Gemini API keys with rotation and quota tracking"""
    
    def __init__(self):
        self.api_keys = self._load_api_keys()
        self.current_key_index = 0
        self.key_usage_count = {}
        self.max_requests_per_key = 60  # Leave some buffer below 500 limit
        self.failed_keys = set()  # Track keys that have failed
        
        # Initialize usage counters
        for key in self.api_keys:
            self.key_usage_count[key] = 0
    
    def _load_api_keys(self):
        """Load all available Gemini API keys from environment variables"""
        keys = []
        
        # Try to load GEMINI_API_KEY_1 through GEMINI_API_KEY_6
        for i in range(1, 7):
            key = os.environ.get(f"GEMINI_API_KEY_{i}")
            if key:
                keys.append(key)
                print(f"Loaded API key #{i}")
        
        # Also try the original GEMINI_API_KEY for backward compatibility
        original_key = os.environ.get("GEMINI_API_KEY")
        if original_key and original_key not in keys:
            keys.append(original_key)
            print("Loaded original GEMINI_API_KEY")
        
        if not keys:
            raise ValueError("No Gemini API keys found in environment variables")
        
        print(f"Total API keys loaded: {len(keys)}")
        return keys
    
    def get_current_key(self):
        """Get the current active API key"""
        if not self.api_keys:
            raise ValueError("No API keys available")
        
        # If current key has failed or exceeded quota, rotate to next
        current_key = self.api_keys[self.current_key_index]
        
        if (current_key in self.failed_keys or 
            self.key_usage_count[current_key] >= self.max_requests_per_key):
            self._rotate_key()
            current_key = self.api_keys[self.current_key_index]
        
        return current_key
    
    def _rotate_key(self):
        """Rotate to the next available API key"""
        attempts = 0
        while attempts < len(self.api_keys):
            self.current_key_index = (self.current_key_index + 1) % len(self.api_keys)
            current_key = self.api_keys[self.current_key_index]
            
            # Check if this key is still usable
            if (current_key not in self.failed_keys and 
                self.key_usage_count[current_key] < self.max_requests_per_key):
                print(f"Rotated to API key #{self.current_key_index + 1}")
                return
            
            attempts += 1
        
        # If we get here, all keys are exhausted
        raise Exception("All API keys have been exhausted or failed")
    
    def increment_usage(self, key):
        """Increment usage count for a specific key"""
        if key in self.key_usage_count:
            self.key_usage_count[key] += 1
            print(f"API key usage: {self.key_usage_count[key]}/{self.max_requests_per_key}")
    
    def mark_key_as_failed(self, key, error_message):
        """Mark a key as failed due to quota or other errors"""
        self.failed_keys.add(key)
        print(f"Marked API key as failed: {error_message}")
        
        # If this was our current key, rotate immediately
        if key == self.api_keys[self.current_key_index]:
            try:
                self._rotate_key()
            except Exception as e:
                print(f"Failed to rotate key: {e}")
    
    def get_status(self):
        """Get status of all API keys"""
        status = {}
        for i, key in enumerate(self.api_keys):
            key_id = f"Key_{i+1}"
            status[key_id] = {
                "usage": self.key_usage_count[key],
                "max_requests": self.max_requests_per_key,
                "failed": key in self.failed_keys,
                "active": i == self.current_key_index
            }
        return status

# Initialize API key manager
api_key_manager = APIKeyManager()

def save_binary_file(file_name, data):
    with open(file_name, "wb") as f:
        f.write(data)

def compress_image(image_path, quality=65):
    try:
        with Image.open(image_path) as img:
            webp_path = f"{os.path.splitext(image_path)[0]}.webp"
            img.save(webp_path, 'WEBP', quality=quality)
            os.remove(image_path)  # Remove original file
            return webp_path
    except Exception as e:
        print(f"Image compression error: {e}")
        return image_path

def upload_to_cloudinary(file_path, resource_type="image"):
    """Upload file to Cloudinary. resource_type can be 'image', 'raw', etc."""
    url = f"https://api.cloudinary.com/v1_1/{os.environ['CLOUDINARY_CLOUD_NAME']}/{resource_type}/upload"
    payload = {
        'upload_preset': 'ml_default',
        'api_key': os.environ['CLOUDINARY_API_KEY']
    }
    try:
        with open(file_path, 'rb') as f:
            files = {'file': f}
            response = requests.post(url, data=payload, files=files)
        if response.status_code == 200:
            return response.json()['secure_url']
        print(f"Upload failed: {response.text}")
        return None
    except Exception as e:
        print(f"Upload error: {e}")
        return None

def generate_and_upload_image(title):
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            current_key = api_key_manager.get_current_key()
            client = genai.Client(api_key=current_key)
            model = "gemini-2.0-flash-exp-image-generation"
            contents = [types.Content(
                role="user",
                parts=[types.Part.from_text(text=f"""Create a realistic and visually appealing blog header image for the topic: "{title}". Include the title text "{title}" overlaid on the image as a stylish, readable heading.Use clean typography that fits the blog's aesthetic.The image size should be in a 16:9 aspect ratio. The title should be clearly visible but not overpower the image.""")]
            )]
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(response_modalities=["image", "text"])
            )

            # Increment usage counter for successful request
            api_key_manager.increment_usage(current_key)

            if response.candidates and response.candidates[0].content.parts:
                inline_data = response.candidates[0].content.parts[0].inline_data
                file_ext = mimetypes.guess_extension(inline_data.mime_type)
                original_file = f"blog_image_{int(time.time())}{file_ext}"
                save_binary_file(original_file, inline_data.data)

                # Compress and convert to WebP
                final_file = compress_image(original_file)
                return upload_to_cloudinary(final_file)
            return None
            
        except Exception as e:
            error_str = str(e).lower()
            
            # Check if it's a quota/resource exhausted error
            if any(keyword in error_str for keyword in ['quota', 'resource_exhausted', 'limit']):
                print(f"Quota exhausted for current API key (attempt {attempt + 1}): {e}")
                api_key_manager.mark_key_as_failed(current_key, str(e))
                
                if attempt < max_retries - 1:
                    print("Retrying with next API key...")
                    time.sleep(2)  # Brief pause before retry
                    continue
            else:
                print(f"Image generation error (attempt {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
            
            # If we reach here on the last attempt, return None
            if attempt == max_retries - 1:
                print("Max retries reached for image generation")
                return None

def create_slug(title):
    """Generate SEO-friendly slug from title"""
    slug = title.lower()
    # Remove special characters and replace spaces with hyphens
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'[\s-]+', '-', slug)
    slug = slug.strip('-')  # Remove leading/trailing hyphens
    slug = slug[:100]  # Limit length
    return slug

def link_to_keywords(link):
    """Convert a link to plain text keywords"""
    # Extract path from URL
    url_path = link.split('beacleaner.com/')[-1].strip('/')
    # Convert hyphens to spaces and remove trailing slash
    keywords = url_path.replace('-', ' ')
    return keywords

def find_relevant_links(target_keyword, links, top_n=TOP_LINKS_COUNT):
    """Find the most relevant links for a given keyword using cosine similarity"""
    if not links:
        return []
    
    # Convert links to plain text keywords
    link_keywords = [link_to_keywords(link) for link in links]
    
    # Add the target keyword to the list of texts
    all_texts = link_keywords + [target_keyword]
    
    # Create TF-IDF vectors
    vectorizer = TfidfVectorizer(stop_words='english')
    tfidf_matrix = vectorizer.fit_transform(all_texts)
    
    # Calculate cosine similarity between the target keyword and all link keywords
    target_vector = tfidf_matrix[-1]  # Last vector is the target keyword
    link_vectors = tfidf_matrix[:-1]  # All other vectors are link keywords
    similarities = cosine_similarity(target_vector, link_vectors).flatten()
    
    # Sort links by similarity score
    link_sim_pairs = list(zip(links, similarities))
    link_sim_pairs.sort(key=lambda x: x[1], reverse=True)
    
    # Return top N most similar links
    top_links = [pair[0] for pair in link_sim_pairs[:top_n]]
    
    print(f"Found {len(top_links)} relevant links for keyword: {target_keyword}")
    for i, (link, sim) in enumerate(link_sim_pairs[:5], 1):
        print(f"  {i}. {link} (similarity: {sim:.4f})")
    
    return top_links

def create_article_prompt(title, article_number, image_url):
    tomorrow = datetime.now() + timedelta(days=1)
    publish_date = tomorrow.strftime("%Y-%m-%d")
    slug = create_slug(title)
    canonical_url = f"https://www.beacleaner.com/{slug}"
    
    # Get existing links from links.txt
    existing_links = get_existing_links()
    
    # Find relevant links using cosine similarity
    relevant_links = find_relevant_links(title, existing_links)
    
    # Format links as a JSON string for the prompt
    links_json = json.dumps(relevant_links, indent=2)

    prompt = f"""Based on the title: "{title}", create a comprehensive, SEO-optimized article:

This will be article #{article_number} in the series.

Create a comprehensive, SEO-optimized article in Markdown format, approximately 2,500–3,000 words in length, following the provided guidelines.

---
publishDate: {publish_date}T00:00:00Z
title: {title}  # Use the exact user-entered title here
excerpt: [Write compelling meta description between 130-145 characters that includes primary keyword]
image: {image_url}
category: [Determine appropriate category based on content]
tags:
  - [related keyword]
  - [secondary keyword]
  - [related keyword]
metadata:
  canonical: {canonical_url}
---

Article Structure Requirements:
1. Title (H2): Include primary keyword near beginning, under 60 characters, compelling and click-worthy
2. Introduction (150-200 words): Open with hook, include primary keyword in first 100 words, establish relevance, outline article content
3. Takeaway: Brief summary of key actionable message in the bullet points
4. Provide a clear, concise answer to the main query in 40-60 words.
5. Main Body: 5-7+ H2 sections with:
   - Section headings using keywords naturally
   - 200-300 words per section
   - Include primary/secondary keywords
   - Use H3 subsections where appropriate
   - Include bullet points or numbered lists
   - Include 3-7 anchor texts links that are contextually relevant to the current content. Choose from these most relevant links based on cosine similarity:
   {links_json}
   - Natural transitions between sections
6. FAQ Section: 4-6 questions based on common search queries, with concise answers (50-75 words each)
7. Conclusion (150-200 words): Summarize main points, restate primary keyword, include clear call-to-action

Ensure the article:
- Uses semantic analysis and NLP techniques for natural keyword inclusion
- Has high readability with varied sentence structures
- Incorporates LSI keywords naturally
- Has proper hierarchy with H2 and H3 tags
- Uses engaging, conversational tone
- Provides unique, valuable insights
- Create content strictly adhering to an NLP-friendly format, emphasizing clarity and simplicity in structure and language. Ensure sentences follow a straightforward subject-verb-object order, selecting words for their precision and avoiding any ambiguity. Exclude filler content, focusing on delivering information succinctly. Do not use complex or abstract terms such as 'meticulous', 'navigating', 'complexities,' 'realm,' 'bespoke,' 'tailored', 'towards,' 'underpins,' 'ever-changing,' 'the world of,' 'not only,' 'seeking more than just,' 'ever-evolving,' 'robust'. This approach aims to streamline content production for enhanced NLP algorithm comprehension, ensuring the output is direct, accessible, and interpretable.
- While prioritizing NLP-friendly content creation (60%), also dedicate 40% of your focus to making the content engaging and enjoyable for readers, balancing technical NLP-optimization with reader satisfaction to produce content that not only ranks well on search engines but also remains compelling and valuable to the audience.
- Write content on [Keyword] in a conversational tone. Explain each idea within three to four sentences. Ensure each sentence is simple, sweet, and to-the-point. Use first-person perspective where appropriate to add a personal touch. Be creative with the starting sentence and bring variations. Ensure you include an intro and a conclusion. Make sure all ideas are fresh, unique, and new.

When inserting links, choose 3-4 that are most contextually relevant to the specific section content. Format the links as proper Markdown anchor text, like this: [anchor text](URL).

Provide the complete article in proper Markdown format.
"""
    return prompt

def generate_article(prompt):
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            current_key = api_key_manager.get_current_key()
            client = genai.Client(api_key=current_key)
            model = "gemini-2.5-flash-preview-05-20"
            contents = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]
            generate_content_config = types.GenerateContentConfig(
                temperature=1,
                top_p=0.95,
                top_k=64,
                max_output_tokens=8192,
                response_mime_type="text/plain",
            )
            
            print(f"Generating article (attempt {attempt + 1})...")
            full_response = ""
            
            # Using streaming response
            for chunk in client.models.generate_content_stream(
                model=model,
                contents=contents,
                config=generate_content_config,
            ):
                if chunk.text:
                    print(chunk.text, end="", flush=True)
                    full_response += chunk.text
            
            # Increment usage counter for successful request
            api_key_manager.increment_usage(current_key)
            
            print("\nArticle generation complete.")
            return full_response if full_response else "No content generated"
            
        except Exception as e:
            error_str = str(e).lower()
            
            # Check if it's a quota/resource exhausted error
            if any(keyword in error_str for keyword in ['quota', 'resource_exhausted', 'limit']):
                print(f"Quota exhausted for current API key (attempt {attempt + 1}): {e}")
                api_key_manager.mark_key_as_failed(current_key, str(e))
                
                if attempt < max_retries - 1:
                    print("Retrying with next API key...")
                    time.sleep(2)  # Brief pause before retry
                    continue
            else:
                print(f"Error generating article (attempt {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
            
            # If we reach here on the last attempt, return error
            if attempt == max_retries - 1:
                return f"Error generating article after {max_retries} attempts: {e}"

def send_email_notification(titles, article_urls, recipient_email="homecosycreation@gmail.com"):
    """Send email notification about generated articles"""
    from_email = "limon.working@gmail.com"
    app_password = os.environ.get("EMAIL_PASSWORD")
    
    if not app_password:
        print("Email password not set. Skipping email notification.")
        return False

    # Create the email
    msg = MIMEMultipart()
    msg['From'] = from_email
    msg['To'] = recipient_email
    msg['Subject'] = f"Generated Articles - {datetime.now().strftime('%Y-%m-%d %H:%M')}"

    # Add API key usage status to email
    api_status = api_key_manager.get_status()
    status_text = "\n\nAPI Key Usage Status:\n"
    for key_id, status in api_status.items():
        status_text += f"{key_id}: {status['usage']}/{status['max_requests']} requests"
        if status['failed']:
            status_text += " (FAILED)"
        if status['active']:
            status_text += " (ACTIVE)"
        status_text += "\n"

    # Add body text
    body = f"The following articles have been generated and committed to the GitHub repository:\n\n"
    for i, (title, url) in enumerate(zip(titles, article_urls), 1):
        body += f"{i}. {title}\n   URL: {url}\n\n"
    
    body += status_text
    
    msg.attach(MIMEText(body, 'plain'))

    # Send the email
    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(from_email, app_password)
        server.send_message(msg)
        server.quit()
        print(f"Email notification sent successfully to {recipient_email}")
        return True
    except Exception as e:
        print(f"Failed to send email notification: {e}")
        return False

def read_keywords_from_csv(filename=KEYWORDS_FILE):
    """Read all keywords from the CSV file"""
    try:
        keywords = []
        if os.path.exists(filename):
            with open(filename, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                for row in reader:
                    if row and row[0].strip():  # Check if row exists and has content
                        keywords.append(row[0].strip())
        else:
            print(f"Keywords file {filename} not found. Creating new file.")
            os.makedirs(os.path.dirname(filename), exist_ok=True)
            with open(filename, 'w', encoding='utf-8'):
                pass
        return keywords
    except Exception as e:
        print(f"Error reading keywords from CSV: {e}")
        return []

def write_keywords_to_csv(keywords, filename=KEYWORDS_FILE):
    """Write keywords back to the CSV file"""
    try:
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        with open(filename, 'w', encoding='utf-8', newline='') as f:
            writer = csv.writer(f)
            for keyword in keywords:
                writer.writerow([keyword])
        return True
    except Exception as e:
        print(f"Error writing keywords to CSV: {e}")
        return False

def append_processed_keywords(keywords, urls, filename=PROCESSED_KEYWORDS_FILE):
    """Append processed keywords to the processed file with timestamps and URLs"""
    try:
        # Create file if it doesn't exist
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        
        # Determine header based on file existence
        file_exists = os.path.exists(filename) and os.path.getsize(filename) > 0
        
        with open(filename, 'a', encoding='utf-8') as f:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # Write header if it's a new file
            if not file_exists:
                f.write("# Processed Keywords Log\n")
                f.write("# Format: [TIMESTAMP] KEYWORD - URL\n\n")
            
            f.write(f"## Batch processed on {timestamp}\n")
            for keyword, url in zip(keywords, urls):
                f.write(f"{url}\n")
            f.write("\n")  # Add a blank line between batches
        return True
    except Exception as e:
        print(f"Error appending to processed keywords file: {e}")
        return False

def append_to_generated_keywords(keywords, filename=GENERATED_KEYWORDS_FILE):
    """Append successfully generated keywords to the keywords-generated.txt file"""
    try:
        # Create file if it doesn't exist
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        
        with open(filename, 'a', encoding='utf-8') as f:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for keyword in keywords:
                f.write(f"{keyword}\n")
        return True
    except Exception as e:
        print(f"Error appending to generated keywords file: {e}")
        return False

def append_to_links_file(old_urls, new_urls, filename=LINKS_FILE):
    """Append both old and new links to the links.txt file, removing duplicates"""
    try:
        # Create file if it doesn't exist
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        
        # Get existing links from file to check for duplicates
        existing_links = set()
        if os.path.exists(filename) and os.path.getsize(filename) > 0:
            with open(filename, 'r', encoding='utf-8') as f:
                existing_links = {line.strip() for line in f if line.strip()}
        
        # Combine and deduplicate links
        all_urls = old_urls + new_urls
        unique_new_urls = [url for url in all_urls if url not in existing_links]
        
        # Only write if there are new unique URLs
        if unique_new_urls:
            with open(filename, 'a', encoding='utf-8') as f:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                # Write only unique URLs
                for url in unique_new_urls:
                    f.write(f"{url}\n")
                f.write("\n")  # Add a blank line between batches
            return True
        else:
            print("No new unique URLs to append")
            return True
    except Exception as e:
        print(f"Error appending to links file: {e}")
        return False

def get_existing_links(filename=LINKS_FILE):
    """Get all existing links from links.txt"""
    existing_links = []
    if os.path.exists(filename):
        with open(filename, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and 'https://' in line:
                    existing_links.append(line)
    return existing_links

def get_keywords(filename=KEYWORDS_FILE, count=ARTICLES_PER_RUN):
    """Get keywords from file and track which ones were used"""
    try:
        # Read all keywords from CSV
        all_keywords = read_keywords_from_csv(filename)
        
        if not all_keywords:
            print("No keywords found in the CSV file or file is empty.")
            # Fallback to default keywords
            return [
                " ",
               
            ], []
        
        # If we don't have enough keywords, use what we have
        if len(all_keywords) <= count:
            selected_keywords = all_keywords.copy()
            return selected_keywords, selected_keywords
        
        # Find the most recently processed index from a tracking file
        last_index = 0
        track_file = ".last_keyword_index"
        if os.path.exists(track_file):
            with open(track_file, 'r') as f:
                try:
                    last_index = int(f.read().strip())
                except ValueError:
                    last_index = 0
        
        # Calculate next batch of keywords
        start_index = last_index % len(all_keywords)
        end_index = start_index + count
        
        # Handle wrapping around the list
        if end_index <= len(all_keywords):
            selected_keywords = all_keywords[start_index:end_index]
        else:
            selected_keywords = all_keywords[start_index:] + all_keywords[:end_index - len(all_keywords)]
        
        # Update tracking file - always reset to 0
        with open(track_file, 'w') as f:
            f.write('0')
        
        return selected_keywords, selected_keywords
        
    except Exception as e:
        print(f"Error reading keywords file: {e}")
        # Fallback to default keywords if we can't read the file
        return [
            " "
        ], []

def update_keyword_files(all_used_keywords, article_urls):
    """Update the keyword files after processing"""
    if not all_used_keywords:
        print("No keywords to update.")
        return
    
    # Read all current keywords
    all_keywords = read_keywords_from_csv(KEYWORDS_FILE)
    
    # Remove used keywords
    remaining_keywords = [k for k in all_keywords if k not in all_used_keywords]
    
    # Write remaining keywords back to Text
    if write_keywords_to_csv(remaining_keywords, KEYWORDS_FILE):
        print(f"Removed {len(all_used_keywords)} used keywords from Text file.")
    else:
        print("Failed to update CSV file with remaining keywords.")
    
    # Append used keywords to processed file with URLs
    if append_processed_keywords(all_used_keywords, article_urls, PROCESSED_KEYWORDS_FILE):
        print(f"Added {len(all_used_keywords)} keywords to processed keywords file with URLs.")
    else:
        print("Failed to update processed keywords file.")
    
    # Append successfully generated keywords to keywords-generated.txt
    if append_to_generated_keywords(all_used_keywords, GENERATED_KEYWORDS_FILE):
        print(f"Added {len(all_used_keywords)} keywords to generated keywords file.")
    else:
        print("Failed to update generated keywords file.")
    
    # Get existing links and append new ones to links.txt
    existing_links = get_existing_links(LINKS_FILE)
    old_links = [
        "https://www.beacleaner.com/how-to-clean-a-ceiling",
    ]
    
    # Filter out old links that are already in the links file
    filtered_old_links = [link for link in old_links if link not in existing_links]
    
    # Filter out new links that are already in the links file
    filtered_new_links = [link for link in article_urls if link not in existing_links]
    
    if filtered_old_links or filtered_new_links:
        if append_to_links_file(filtered_old_links, filtered_new_links, LINKS_FILE):
            print(f"Added {len(filtered_old_links)} old links and {len(filtered_new_links)} new links to links file.")
        else:
            print("Failed to update links file.")

def main():
    print(f"Starting article generation at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"API Key Manager Status:")
    for key_id, status in api_key_manager.get_status().items():
        print(f"  {key_id}: {'Active' if status['active'] else 'Standby'}")
    
    # Get keywords for this run
    keywords, keywords_to_track = get_keywords()
    print(f"Selected {len(keywords)} keywords for processing")
    
    generated_files = []
    successful_keywords = []
    article_urls = []
    default_image_url = "https://res.cloudinary.com/dbcpfy04c/image/upload/v1743184673/images_k6zam3.png"

    for i, title in enumerate(keywords, 1):
        print(f"\n{'='*50}\nGenerating Article #{i}: {title}\n{'='*50}")

        try:
            # Generate slug and URL
            slug = create_slug(title)
            article_url = f"https://beacleaner.com/{slug}"
            
            # Generate image
            image_url = generate_and_upload_image(title) or default_image_url
            print(f"Generated image URL: {image_url}")

            # Create prompt with actual image URL
            prompt = create_article_prompt(title, i, image_url)

            # Generate article
            article = generate_article(prompt)
            if article.startswith("Error"):
                print("Article generation failed, skipping to next keyword")
                continue

            # Save article
            filename = f"{OUTPUT_DIR}/{slug}.md"
            with open(filename, "w", encoding="utf-8") as f:
                f.write(article)
                
            generated_files.append(filename)
            successful_keywords.append(title)
            article_urls.append(article_url)
            print(f"Article saved to {filename}")
            print(f"Article URL: {article_url}")

        except Exception as e:
            print(f"Error processing keyword '{title}': {e}")
            continue

        if i < len(keywords):
            print("Waiting 10 seconds before next article...")
            time.sleep(10)
    
    # Print final API key usage status
    print(f"\nFinal API Key Usage Status:")
    for key_id, status in api_key_manager.get_status().items():
        print(f"  {key_id}: {status['usage']}/{status['max_requests']} requests"
              f"{' (FAILED)' if status['failed'] else ''}")
    
    # Update keyword files only for successfully processed keywords
    update_keyword_files(successful_keywords, article_urls)
    
    # Send email notification with URLs
    if generated_files:
        send_email_notification(successful_keywords, article_urls)
    
    print(f"Article generation completed at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Generated {len(generated_files)} articles")
    print(f"Successfully processed {len(successful_keywords)} keywords")
    
    for i, (keyword, url) in enumerate(zip(successful_keywords, article_urls), 1):
        print(f"{i}. {keyword} → {url}")

if __name__ == "__main__":
    main()
