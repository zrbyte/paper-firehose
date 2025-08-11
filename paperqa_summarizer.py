#!/usr/bin/env python3
"""
PaperQA2 PDF Summarizer

This script downloads a PDF from arXiv and uses PaperQA2 to generate a comprehensive summary.
Usage: python paperqa_summarizer.py [arxiv_id_or_url]
"""

import os
import sys
import re
import asyncio
from pathlib import Path
from urllib.parse import urlparse
import requests
from paperqa import Docs


def load_api_key(path: str = "openaikulcs.env") -> str | None:
    """Load OpenAI API key from environment variable or file."""
    env_key = os.getenv("OPENAI_API_KEY")
    if env_key:
        return env_key
    try:
        if not os.path.isabs(path):
            path = os.path.join(os.path.dirname(__file__), path)
        with open(path, "r", encoding="utf-8") as f:
            key = f.read().strip()
            if "=" in key:
                key = key.split("=", 1)[-1].strip()
            return key
    except Exception:
        return None


def extract_arxiv_id(url_or_id: str) -> str:
    """Extract arXiv ID from URL or return the ID if already in correct format."""
    # Handle direct arXiv ID format like "2506.20738"
    arxiv_pattern = r'^\d{4}\.\d{4,5}(?:v\d+)?$'
    if re.match(arxiv_pattern, url_or_id):
        return url_or_id
    
    # Extract from arXiv URLs
    patterns = [
        r'arxiv\.org/abs/(\d{4}\.\d{4,5}(?:v\d+)?)',
        r'arxiv\.org/pdf/(\d{4}\.\d{4,5}(?:v\d+)?)',
        r'(\d{4}\.\d{4,5}(?:v\d+)?)'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url_or_id)
        if match:
            return match.group(1)
    
    raise ValueError(f"Could not extract arXiv ID from: {url_or_id}")


def download_arxiv_pdf(arxiv_id: str, output_dir: str = ".") -> str:
    """Download PDF from arXiv and return the local file path."""
    # Remove version number for the URL if present
    clean_id = re.sub(r'v\d+$', '', arxiv_id)
    
    pdf_url = f"https://arxiv.org/pdf/{clean_id}.pdf"
    local_filename = os.path.join(output_dir, f"{clean_id}.pdf")
    
    print(f"Downloading PDF from: {pdf_url}")
    
    try:
        response = requests.get(pdf_url, stream=True)
        response.raise_for_status()
        
        with open(local_filename, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        print(f"PDF downloaded successfully: {local_filename}")
        return local_filename
        
    except requests.exceptions.RequestException as e:
        raise Exception(f"Failed to download PDF: {e}")


async def summarize_paper_async(pdf_path: str, custom_questions: list = None) -> dict:
    """Use PaperQA2 to analyze the paper and answer questions."""
    
    # Initialize PaperQA2 Docs object
    docs = Docs()
    
    print(f"Adding PDF to PaperQA2: {pdf_path}")
    try:
        # Add the PDF to the docs
        await docs.aadd(pdf_path)
    except Exception as e:
        print(f"Error adding PDF: {e}")
        # Try synchronous version as fallback
        docs.add(pdf_path)
    
    # Default questions for comprehensive analysis
    default_questions = [
        "What is the main research question or objective of this paper?",
        "What are the key findings and results?", 
        "What methods or techniques were used in this research?",
        "What are the main conclusions and implications?",
        "What are the limitations or areas for future work mentioned?"
    ]
    
    questions = custom_questions if custom_questions else default_questions
    
    results = {}
    
    for question in questions:
        print(f"Asking: {question}")
        try:
            answer = await docs.aquery(question)
            # Extract answer text from response object
            if hasattr(answer, 'answer'):
                results[question] = answer.answer
            elif hasattr(answer, 'text'):
                results[question] = answer.text
            else:
                results[question] = str(answer)
        except Exception as e:
            try:
                # Fallback to synchronous version
                answer = docs.query(question)
                if hasattr(answer, 'answer'):
                    results[question] = answer.answer
                elif hasattr(answer, 'text'):
                    results[question] = answer.text
                else:
                    results[question] = str(answer)
            except Exception as e2:
                results[question] = f"Error generating answer: {e2}"
        
        print(f"✓ Answer received")
    
    return results


def summarize_paper(pdf_path: str, custom_questions: list = None) -> dict:
    """Use PaperQA2 to analyze the paper and answer questions synchronously."""
    
    # Initialize PaperQA2 Docs object
    docs = Docs()
    
    print(f"Adding PDF to PaperQA2: {pdf_path}")
    docs.add(pdf_path)
    
    # Default questions for comprehensive analysis
    default_questions = [
        "What is the main research question or objective of this paper?",
        "What are the key findings and results?", 
        "What methods or techniques were used in this research?",
        "What are the main conclusions and implications?",
        "What are the limitations or areas for future work mentioned?"
    ]
    
    questions = custom_questions if custom_questions else default_questions
    
    results = {}
    
    for question in questions:
        print(f"Asking: {question}")
        try:
            # Use async query but run in new event loop
            answer = asyncio.run(docs.aquery(question))
            # Extract answer text from response object
            if hasattr(answer, 'answer'):
                results[question] = answer.answer
            elif hasattr(answer, 'text'):
                results[question] = answer.text
            else:
                results[question] = str(answer)
            print(f"✓ Answer received")
        except Exception as e:
            results[question] = f"Error generating answer: {e}"
            print(f"✗ Error: {e}")
    
    return results


def format_summary(results: dict, arxiv_id: str) -> str:
    """Format the analysis results into a readable summary."""
    
    summary = f"""
{'='*80}
PAPERQA SUMMARY - arXiv:{arxiv_id}
{'='*80}

"""
    
    for question, answer in results.items():
        summary += f"""
{'-'*60}
Q: {question}
{'-'*60}
A: {answer}

"""
    
    summary += f"{'='*80}\n"
    
    return summary


def main():
    """Main function to run the PaperQA summarizer."""
    
    # Default to the magic angle twisted bilayer graphene paper if no argument provided
    default_paper = "2506.20738"
    
    if len(sys.argv) > 1:
        input_arg = sys.argv[1]
    else:
        input_arg = default_paper
        print(f"No arXiv ID provided, using default: {default_paper}")
    
    try:
        # Extract arXiv ID
        arxiv_id = extract_arxiv_id(input_arg)
        print(f"Processing arXiv paper: {arxiv_id}")
        
        # Create output directory
        output_dir = "paperqa_downloads"
        os.makedirs(output_dir, exist_ok=True)
        
        # Download PDF
        pdf_path = download_arxiv_pdf(arxiv_id, output_dir)
        
        # Load and set API key
        api_key = load_api_key()
        if not api_key:
            print("\nERROR: OpenAI API key not found.")
            print("Please ensure either:")
            print("1. OPENAI_API_KEY environment variable is set, or")
            print("2. openaikulcs.env file exists in the current directory")
            return
        
        # Set the environment variable for PaperQA2
        os.environ["OPENAI_API_KEY"] = api_key
        print("✓ OpenAI API key loaded successfully")
        
        # Analyze paper with PaperQA2
        print("\nAnalyzing paper with PaperQA2...")
        results = summarize_paper(pdf_path)
        
        # Format and display results
        summary = format_summary(results, arxiv_id)
        print(summary)
        
        # Save summary to file
        summary_file = os.path.join(output_dir, f"{arxiv_id}_summary.txt")
        with open(summary_file, 'w', encoding='utf-8') as f:
            f.write(summary)
        
        print(f"Summary saved to: {summary_file}")
        
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
