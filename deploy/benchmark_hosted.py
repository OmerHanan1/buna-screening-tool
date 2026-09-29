"""Generate an original long manuscript; never reads a user manuscript."""
import argparse
from pathlib import Path
import random
import pymupdf


def manuscript(path: Path, pages: int = 45, dense: bool = False):
    rng = random.Random(17)
    subjects = ["participants", "observers", "researchers", "students", "volunteers"]
    topics = ["emotional regulation", "social distance", "moral judgments", "memory retrieval", "attention", "cognitive flexibility"]
    sentences = [
        "The {subject} evaluated {topic} during repeated laboratory sessions using carefully balanced picture sets.",
        "We examined whether the effects of psychological distance on emotion depended on the task context and the order of presentation.",
        "Each rating was recorded before the next trial, and the resulting observations were summarized separately for the two experimental conditions.",
        "The relationship between individual differences and response variability was assessed with a model that included a participant-specific intercept.",
        "Our original synthetic design deliberately separates changes in reported intensity from changes in the interpretation of the underlying event.",
        "For this comparison, the {subject} considered both immediate consequences and possible future outcomes while maintaining the same response scale.",
        "These observations do not establish a general causal explanation because the present procedure manipulates only a limited set of contextual features.",
        "The {subject} were asked to rate the emotional intensity of each image before explaining their choice in a short written response.",
        "A second assessment repeated the main question with different examples, allowing us to inspect consistency across separate measurement occasions.",
        "The analysis retained all eligible responses and documented missing observations without replacing them with estimated values.",
    ]
    document = pymupdf.open()
    for i in range(pages):
        page = document.new_page()
        if i == 0:
            heading = "Synthetic manuscript for bounded runtime verification"
        elif i == 1:
            heading = "Abstract"
        elif i < 10:
            heading = "Introduction" if i == 2 else ""
        elif i < 25:
            heading = "Methods" if i == 10 else ""
        elif i < 35:
            heading = "Results" if i == 25 else ""
        else:
            heading = "Discussion" if i == 35 else ""
        paragraphs = []
        for _ in range(5):
            paragraph = " ".join(rng.choice(sentences).format(subject=rng.choice(subjects), topic=rng.choice(topics)) for _ in range(3))
            if dense:
                paragraph = ("We examined the level of construal involved in the elicitation of core versus moral disgust. "
                             "The analysis considered emotion regulation by psychological distance and level of abstraction. "
                             + rng.choice(sentences).format(subject=rng.choice(subjects), topic=rng.choice(topics)))
            paragraphs.append(paragraph)
        text = (heading + "\n\n" if heading else "") + "\n\n".join(paragraphs)
        remaining = page.insert_textbox(pymupdf.Rect(50, 45, 545, 785), text, fontsize=10, lineheight=1.4)
        if remaining < 0:
            raise RuntimeError("Synthetic page overflow; benchmark cannot silently omit text.")
        page.insert_text((290, 820), str(i + 1), fontsize=8)
    document.save(path)
    document.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--pages", type=int, default=45)
    parser.add_argument("--dense", action="store_true")
    args = parser.parse_args()
    manuscript(args.path, args.pages, args.dense)
