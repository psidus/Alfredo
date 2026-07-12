import os
import io
import time
from typing import List
import logging
from langchain.docstore.document import Document

logger = logging.getLogger(__name__)

class MistralOCRParser:
    """
    Parser for PDFs using Mistral's OCR API (mistral-ocr-latest).
    Extracts text and tables natively into Markdown.
    """
    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.environ.get("MISTRAL_API_KEY")
        if not self.api_key:
            raise ValueError("MISTRAL_API_KEY is not set. Please set it in Settings.")
        
        try:
            from mistralai.client import Mistral
            self.client = Mistral(api_key=self.api_key)
        except ImportError:
            raise ImportError("Please install mistralai package: pip install mistralai")

    def parse_pdf(self, file_path: str) -> List[Document]:
        """
        Parses a PDF using Mistral OCR API and returns a LangChain Document 
        with the full Markdown content.
        """
        logger.info(f"Uploading {file_path} to Mistral API for OCR...")
        
        try:
            # 1. Upload file
            with open(file_path, "rb") as f:
                uploaded_file = self.client.files.upload(
                    file={
                        "file_name": os.path.basename(file_path),
                        "content": f.read(),
                    },
                    purpose="ocr"
                )
            
            logger.info(f"File uploaded successfully to Mistral. ID: {uploaded_file.id}")
            
            # 2. Get signed URL
            signed_url = self.client.files.get_signed_url(file_id=uploaded_file.id)
            
            # 3. Process OCR
            logger.info("Starting Mistral OCR processing (this may take a while)...")
            ocr_response = self.client.ocr.process(
                model="mistral-ocr-latest",
                document={
                    "type": "document_url",
                    "document_url": signed_url.url,
                }
            )
            
            # 4. Extract Markdown
            full_markdown = ""
            if hasattr(ocr_response, 'pages'):
                for page in ocr_response.pages:
                    if hasattr(page, 'markdown'):
                        full_markdown += f"\n\n{page.markdown}"
            
            # Optional cleanup: Delete file from Mistral storage after OCR
            try:
                self.client.files.delete(file_id=uploaded_file.id)
                logger.info(f"Cleaned up file {uploaded_file.id} from Mistral storage.")
            except Exception as e:
                logger.warning(f"Failed to delete file from Mistral storage: {e}")

            if not full_markdown.strip():
                logger.warning("Mistral OCR returned empty markdown.")
                
            return [Document(
                page_content=full_markdown.strip(), 
                metadata={"source": file_path, "parser": "mistral_ocr"}
            )]
            
        except Exception as e:
            logger.error(f"Mistral OCR failed: {e}")
            raise RuntimeError(f"Mistral OCR processing failed: {e}")
