"""File upload helpers for application forms."""

from pathlib import Path
from typing import Optional

from rich.console import Console

console = Console(force_terminal=True)


def handle_file_uploads(page, resume_file: Optional[Path], cl_file: Optional[Path]):
    """Handle file upload fields and attach resume / cover letter files."""
    file_inputs = page.query_selector_all('input[type="file"]')
    generic_upload_idx = 0

    for file_input in file_inputs:
        label = page.evaluate("""(el) => {
            if (el.id) {
                const label = document.querySelector(`label[for="${el.id}"]`);
                if (label) return label.textContent.trim().toLowerCase();
            }
            const parent = el.closest('label, .field, .form-group, [class*="upload"], [class*="attachment"]');
            if (parent) {
                const heading = parent.querySelector('h3, h4, label, .field-label, [class*="label"]');
                if (heading) return heading.textContent.trim().toLowerCase();
                return parent.textContent.trim().toLowerCase().slice(0, 200);
            }
            return '';
        }""", file_input)

        try:
            upload_file: Optional[Path] = None
            upload_label = "file"
            if any(kw in label for kw in ["resume", "cv", "curriculum"]):
                upload_file = resume_file
                upload_label = "resume"
            elif any(kw in label for kw in ["cover letter", "cover_letter", "coverletter"]):
                upload_file = cl_file
                upload_label = "cover letter"
            else:
                if generic_upload_idx == 0:
                    upload_file = resume_file
                    upload_label = f"resume (position {generic_upload_idx + 1})"
                elif generic_upload_idx == 1:
                    upload_file = cl_file
                    upload_label = f"cover letter (position {generic_upload_idx + 1})"
                else:
                    upload_file = resume_file
                    upload_label = "file (defaulting to resume)"
                generic_upload_idx += 1

            if upload_file and upload_file.exists():
                uploaded = False
                trigger_texts = [
                    "From Device", "Browse", "Choose File", "Upload", "Attach", "Select File",
                ]
                for text in trigger_texts:
                    try:
                        trigger = page.get_by_role("button", name=text, exact=False).first
                        if trigger.is_visible(timeout=500):
                            with page.expect_file_chooser(timeout=5000) as fc_info:
                                trigger.click()
                            fc_info.value.set_files(str(upload_file))
                            page.wait_for_timeout(4000)
                            console.print(f"  Uploaded {upload_label} via file chooser ({text}): {upload_file.name}")
                            uploaded = True
                            break
                    except Exception:
                        continue
                if not uploaded:
                    file_input.set_input_files(str(upload_file))
                    page.wait_for_timeout(1500)
                    console.print(f"  Uploaded {upload_label}: {upload_file.name}")
        except Exception as e:
            if "navigation" in str(e).lower() or "destroyed" in str(e).lower():
                console.print("  [dim]Upload triggered page navigation -- continuing[/]")
                return
            console.print(f"  [yellow]File upload failed: {e}[/]")
