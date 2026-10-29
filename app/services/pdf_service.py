import os
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime
from loguru import logger
from fpdf import FPDF

PDF_DIR = Path("app/static/uploads/pdfs")
PDF_DIR.mkdir(parents=True, exist_ok=True)


class ConversationPDF(FPDF):
    """Custom FPDF class for CallZenza / Muri AI Conversation Documents."""

    def __init__(self, session_id: str, title: str = "CALLZENZA / MURI AI CONVERSATION"):
        super().__init__()
        self.session_id = session_id
        self.doc_title = title
        self.set_auto_page_break(auto=True, margin=15)

    def header(self):
        # Header banner
        self.set_font("Helvetica", "B", 14)
        self.set_text_color(30, 41, 59) # Slate-800
        self.cell(0, 8, self.doc_title, new_x="LMARGIN", new_y="NEXT", align="L")
        
        self.set_font("Helvetica", "", 9)
        self.set_text_color(100, 116, 139) # Slate-500
        self.cell(0, 5, f"Session: {self.session_id}", new_x="LMARGIN", new_y="NEXT", align="L")
        
        # Horizontal line
        self.set_draw_color(226, 232, 240) # Slate-200
        self.set_line_width(0.5)
        self.line(10, self.get_y() + 2, 200, self.get_y() + 2)
        self.ln(6)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(148, 163, 184) # Slate-400
        self.cell(0, 10, f"Page {self.page_no()} | CallZenza Engine 2026", align="C")


class ConversationPDFService:
    """Service to generate and update per-conversation session PDF documents."""

    @classmethod
    def sanitize_text(cls, text: str) -> str:
        """Sanitizes unicode text to latin-1 safe encoding for standard PDF rendering."""
        if not text:
            return ""
        # Replace common smart quotes, dashes, etc.
        replacements = {
            "\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'",
            "\u2013": "-", "\u2014": "-", "\u2022": "*", "\u2026": "...",
            "\u00a0": " ", "\u00a9": "(c)", "\u00ae": "(r)"
        }
        for orig, repl in replacements.items():
            text = text.replace(orig, repl)
        
        # Encode to latin-1 with replacement to avoid FPDF UnicodeEncodeError
        return text.encode("latin-1", "replace").decode("latin-1")

    @classmethod
    def generate_pdf(
        cls, 
        conversation_id: str, 
        messages: List[Dict[str, Any]], 
        title: str = "CALLZENZA / MURI AI CONVERSATION"
    ) -> str:
        """
        Regenerates/updates the PDF for a conversation using its complete message history.
        Pairs 'user' messages with subsequent 'assistant' messages into Question N / Answer N blocks.
        """
        filename = f"conversation_{conversation_id}.pdf"
        file_path = PDF_DIR / filename

        try:
            pdf = ConversationPDF(session_id=conversation_id, title=title)
            pdf.add_page()

            # Group messages into Q&A pairs
            qa_pairs = []
            current_q = None
            current_q_time = None

            for msg in messages:
                sender = msg.get("sender", "").lower()
                content = msg.get("content", "").strip()
                created_at = msg.get("created_at") or ""

                if sender == "user":
                    if current_q:
                        # Unanswered question fallback
                        qa_pairs.append({
                            "question": current_q,
                            "q_time": current_q_time,
                            "answer": "No answer recorded.",
                            "a_time": ""
                        })
                    current_q = content
                    current_q_time = created_at
                elif sender == "assistant" and current_q:
                    qa_pairs.append({
                        "question": current_q,
                        "q_time": current_q_time,
                        "answer": content,
                        "a_time": created_at
                    })
                    current_q = None
                    current_q_time = None

            if current_q:
                qa_pairs.append({
                    "question": current_q,
                    "q_time": current_q_time,
                    "answer": "Processing...",
                    "a_time": ""
                })

            if not qa_pairs:
                pdf.set_font("Helvetica", "I", 10)
                pdf.set_text_color(148, 163, 184)
                pdf.cell(0, 10, "No messages recorded in this conversation yet.", new_x="LMARGIN", new_y="NEXT")
            else:
                for idx, qa in enumerate(qa_pairs, start=1):
                    # Question Block
                    pdf.set_x(10)
                    pdf.set_font("Helvetica", "B", 11)
                    pdf.set_text_color(79, 70, 229) # Indigo-600
                    q_time_str = f" ({qa['q_time'][:16].replace('T', ' ')})" if qa.get("q_time") else ""
                    pdf.multi_cell(190, 6, cls.sanitize_text(f"Question {idx}{q_time_str}:"), new_x="LMARGIN", new_y="NEXT")

                    pdf.set_x(10)
                    pdf.set_font("Helvetica", "", 10)
                    pdf.set_text_color(30, 41, 59) # Slate-800
                    pdf.multi_cell(190, 5, cls.sanitize_text(qa["question"]), new_x="LMARGIN", new_y="NEXT")
                    pdf.ln(2)

                    # Answer Block
                    pdf.set_x(10)
                    pdf.set_font("Helvetica", "B", 11)
                    pdf.set_text_color(16, 185, 129) # Emerald-600
                    a_time_str = f" ({qa['a_time'][:16].replace('T', ' ')})" if qa.get("a_time") else ""
                    pdf.multi_cell(190, 6, cls.sanitize_text(f"Answer {idx}{a_time_str}:"), new_x="LMARGIN", new_y="NEXT")

                    pdf.set_x(10)
                    pdf.set_font("Helvetica", "", 10)
                    pdf.set_text_color(51, 65, 85) # Slate-700
                    pdf.multi_cell(190, 5, cls.sanitize_text(qa["answer"]), new_x="LMARGIN", new_y="NEXT")
                    
                    # Separator line between Q&A blocks
                    if idx < len(qa_pairs):
                        pdf.ln(3)
                        pdf.set_draw_color(241, 245, 249) # Slate-100
                        pdf.set_line_width(0.3)
                        pdf.line(10, pdf.get_y(), 200, pdf.get_y())
                        pdf.ln(4)

            pdf.output(str(file_path))
            logger.info(f"Generated conversation PDF: {file_path}")
            return f"/api/chatbot/conversations/{conversation_id}/pdf"

        except Exception as e:
            logger.error(f"Failed to generate conversation PDF for {conversation_id}: {e}")
            raise e
