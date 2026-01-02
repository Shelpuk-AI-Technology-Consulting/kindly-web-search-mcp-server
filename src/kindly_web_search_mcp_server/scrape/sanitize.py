import re

def sanitize_markdown(markdown: str) -> str:
    """
    Cleans up the markdown content by removing excessive newlines and whitespace.
    """
    # Replace multiple newlines with a single one
    markdown = re.sub(r'\n{3,}', '\n\n', markdown)
    # Replace multiple spaces with a single one, but not spaces at the start of a line
    markdown = re.sub(r'(?<!^)[ ]{2,}', ' ', markdown)
    # Remove leading/trailing whitespace from each line
    markdown = '\n'.join(line.strip() for line in markdown.split('\n'))
    return markdown.strip()
