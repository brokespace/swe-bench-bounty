from pydantic import BaseModel, Field
from typing import Optional, List
from enum import Enum

class SubmissionType(str, Enum):
    FILE = "file"
    LINK = "link" 
    TEXT = "text"


class FileInfo(BaseModel):
    filename: str
    mime_type: str
    size: int


class SubmissionData(BaseModel):
    job_id: str = Field(..., description="Unique identifier for the scoring job")
    submission_id: str = Field(..., description="Unique identifier for the submission")
    submission_type: SubmissionType = Field(..., description="Type of submission")
    title: Optional[str] = Field(None, description="Submission title")
    description: Optional[str] = Field(None, description="Submission description")
    content: Optional[str] = Field(None, description="Text content or URL link")
    file_info: Optional[FileInfo] = Field(None, description="File information for file submissions")
    file_data: Optional[str] = Field(None, description="Base64-encoded binary file data - provided by watcher for file submissions")
    file_name: Optional[str] = Field(None, description="Original filename if file submission")

