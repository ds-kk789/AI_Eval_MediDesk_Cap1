"""Chunk knowledge.txt, embed locally (MiniLM), index in FAISS, search by role.

Ported from AI_Eval_Module_4_3_Steps/backend/rag_pipeline.py. Every chunk
keeps the visibility label of the [VISIBILITY: ...] block it came from, and
every read path filters on it, so a front-desk request never hands patient
text to the model (BR-5).

Two read paths:

- search(question, role, scope): semantic search for clinic policy. FAISS is
  over-fetched (TOP_K * OVERFETCH) and chunks outside role ∩ scope are
  dropped before anything is returned.
- lookup_patient(patient, role): exact lookup of ONE record by patient ID or
  full name. Patient records are a lookup, not a search, so a question like
  "all diabetic patients" has nothing to match (BR-7).

Embeddings are normalised and indexed with inner product, so each result's
`score` is cosine similarity (drift_check.py uses the top-1 score).

The index rebuilds itself when knowledge.txt or the chunk settings change, so
the v2 regression change (CHUNK_SIZE 500 -> 300) can't run against a stale
index.
"""

import hashlib
import json
import re
import threading
from pathlib import Path

import faiss
import numpy as np
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from backend import config


EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

VISIBILITY_MARKER = re.compile(r"^\[VISIBILITY:\s*(\w+)\s*\]\s*$", re.MULTILINE)
SECTION_MARKER = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)

PATIENT_SECTION = re.compile(r"^PATIENT RECORD (P-\d{4})$")
PATIENT_ID = re.compile(r"\bP-?\s?(\d{4})\b", re.IGNORECASE)
PATIENT_FIELD = re.compile(
    r"(Name|Date of birth|Diagnosis|Current medication|Allergies|Last visit):"
    r"\s*(.+?)\.(?=\s|$)",
    re.DOTALL,
)
FIELD_KEYS = {
    "Name": "name",
    "Date of birth": "date_of_birth",
    "Diagnosis": "diagnosis",
    "Current medication": "medication",
    "Allergies": "allergies",
    "Last visit": "last_visit",
}


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


class RAGPipeline:

    def __init__(self, knowledge_file=None, index_dir=None):
        self.knowledge_file = Path(knowledge_file or config.KNOWLEDGE_FILE)
        self.index_dir = Path(index_dir or config.INDEX_DIR)

        self._embedding_model = None
        self._load_lock = threading.Lock()

        # Parallel to the FAISS index: records[i] describes vector i.
        self.records = []
        self.by_id = {}
        self.index = None

        # patient ID -> {"name": ..., "section": ...}, from the CLINICAL block
        self.patients = {}

    @property
    def embedding_model(self):
        # Loaded on first use: the API answers 401/403 without paying the
        # model load, and importing this module stays cheap.
        if self._embedding_model is None:
            self._embedding_model = HuggingFaceEmbeddings(
                model_name=EMBEDDING_MODEL,
                encode_kwargs={"normalize_embeddings": True},
            )
        return self._embedding_model

    # ------------------------------------------------
    # PARSE DOCUMENT
    # ------------------------------------------------

    def load_segments(self):
        """Split knowledge.txt into (text, visibility) segments."""
        text = self.knowledge_file.read_text(encoding="utf-8")
        matches = list(VISIBILITY_MARKER.finditer(text))

        if not matches:
            return [(text, config.PUBLIC)]

        segments = []

        # Anything before the first marker is public by default.
        preamble = text[: matches[0].start()].strip()
        if preamble:
            segments.append((preamble, config.PUBLIC))

        for i, match in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[match.end():end].strip()

            if body:
                segments.append((body, match.group(1).strip().lower()))

        return segments

    def load_sections(self):
        """(section, visibility, text) for every '## ' section."""
        sections = []

        for body, visibility in self.load_segments():
            headings = list(SECTION_MARKER.finditer(body))

            if not headings:
                sections.append(("PREAMBLE", visibility, body))
                continue

            lead = body[: headings[0].start()].strip()
            if lead:
                sections.append(("PREAMBLE", visibility, lead))

            for i, heading in enumerate(headings):
                end = (
                    headings[i + 1].start()
                    if i + 1 < len(headings)
                    else len(body)
                )
                sections.append(
                    (heading.group(1), visibility, body[heading.start():end].strip())
                )

        return sections

    def create_records(self):
        """Chunk each section separately so visibility is never mixed."""
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=config.CHUNK_SIZE,
            chunk_overlap=config.CHUNK_OVERLAP,
            separators=["\n\n", "\n", ". ", " ", ""],
        )

        records = []

        for section, visibility, text in self.load_sections():
            chunks = [c.strip() for c in splitter.split_text(text) if c.strip()]

            for n, chunk in enumerate(chunks, start=1):
                records.append(
                    {
                        "id": f"{visibility}/{_slug(section)}-{n:02d}",
                        "text": chunk,
                        "visibility": visibility,
                        "section": section,
                    }
                )

        return records

    def _fingerprint(self):
        """What the index was built from; any change forces a rebuild."""
        knowledge = self.knowledge_file.read_bytes()

        return {
            "knowledge_sha256": hashlib.sha256(knowledge).hexdigest(),
            "chunk_size": config.CHUNK_SIZE,
            "chunk_overlap": config.CHUNK_OVERLAP,
            "embedding_model": EMBEDDING_MODEL,
        }

    # ------------------------------------------------
    # BUILD / LOAD INDEX
    # ------------------------------------------------

    def build_index(self):
        self.index_dir.mkdir(parents=True, exist_ok=True)

        records = self.create_records()
        counts = {}
        for record in records:
            counts[record["visibility"]] = counts.get(record["visibility"], 0) + 1

        print(f"Created {len(records)} chunks {counts}")

        embeddings = np.array(
            self.embedding_model.embed_documents([r["text"] for r in records]),
            dtype="float32",
        )

        index = faiss.IndexFlatIP(embeddings.shape[1])
        index.add(embeddings)

        faiss.write_index(index, str(self.index_dir / "index.faiss"))
        (self.index_dir / "chunks.json").write_text(
            json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (self.index_dir / "meta.json").write_text(
            json.dumps(self._fingerprint(), indent=2), encoding="utf-8"
        )

        print(f"FAISS index built: {index.ntotal} vectors in {self.index_dir}")

        self._activate(index, records)
        return records

    def _index_is_current(self):
        meta_path = self.index_dir / "meta.json"

        if not all(
            (self.index_dir / name).exists()
            for name in ("index.faiss", "chunks.json", "meta.json")
        ):
            return False

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return meta == self._fingerprint()

    def load_index(self):
        """Load the index from disk, rebuilding it first if it is stale."""
        with self._load_lock:
            if self.index is not None:
                return

            if not self._index_is_current():
                print("FAISS index missing or stale; rebuilding.")
                self.build_index()
                return

            index = faiss.read_index(str(self.index_dir / "index.faiss"))
            records = json.loads(
                (self.index_dir / "chunks.json").read_text(encoding="utf-8")
            )

            if index.ntotal != len(records):
                raise ValueError(
                    f"Index and chunk metadata out of sync ({index.ntotal} "
                    f"vectors vs {len(records)} records). Delete "
                    f"{self.index_dir} and rebuild."
                )

            self._activate(index, records)

    def _activate(self, index, records):
        self.index = index
        self.records = records
        self.by_id = {record["id"]: record for record in records}

        self.patients = {}
        for section, visibility, text in self.load_sections():
            match = PATIENT_SECTION.match(section)
            if visibility == config.CLINICAL and match:
                fields = self._parse_fields(text)
                self.patients[match.group(1)] = {
                    "section": section,
                    "name": fields.get("name", ""),
                    "fields": fields,
                }

    def _ensure_loaded(self):
        if self.index is None:
            self.load_index()

    # ------------------------------------------------
    # POLICY SEARCH
    # ------------------------------------------------

    def search(self, question, role, top_k=None, scope=None):
        """Chunks the role may see, nearest first.

        `scope` narrows further to what one tool is for: the policy tool
        passes {PUBLIC} so even a clinician's policy search can't surface
        patient records. The effective filter is role ∩ scope; an unknown
        role sees PUBLIC only.
        """
        self._ensure_loaded()

        top_k = top_k or config.TOP_K
        allowed = set(config.ROLE_VISIBILITY.get(role, {config.PUBLIC}))
        if scope is not None:
            allowed &= set(scope)

        if not allowed:
            return []

        vector = np.array(
            [self.embedding_model.embed_query(question)], dtype="float32"
        )
        fetch = min(max(top_k * config.OVERFETCH, top_k), self.index.ntotal)
        scores, indices = self.index.search(vector, fetch)

        results = []

        for score, idx in zip(scores[0], indices[0]):
            if idx == -1:
                continue

            record = self.records[idx]

            if record["visibility"] not in allowed:
                continue

            results.append(self._result(record, float(score)))

            if len(results) >= top_k:
                break

        return results

    # ------------------------------------------------
    # PATIENT LOOKUP
    # ------------------------------------------------

    def resolve_patient(self, patient):
        """Patient ID for an exact ID or full name, else None.

        Raises ValueError if the text names more than one patient ID.
        """
        self._ensure_loaded()

        ids = {f"P-{digits}" for digits in PATIENT_ID.findall(patient or "")}

        if len(ids) > 1:
            raise ValueError("More than one patient ID given.")

        if ids:
            patient_id = ids.pop()
            return patient_id if patient_id in self.patients else None

        wanted = " ".join((patient or "").split()).casefold()

        for patient_id, info in self.patients.items():
            if wanted and info["name"].casefold() == wanted:
                return patient_id

        return None

    def lookup_patient(self, patient, role):
        """All chunks of ONE patient's record, or [] if the role may not see
        them or no record matches. The role check comes first, so a caller
        without CLINICAL access learns nothing about who exists."""
        if config.CLINICAL not in config.ROLE_VISIBILITY.get(role, set()):
            return []

        patient_id = self.resolve_patient(patient)

        if patient_id is None:
            return []

        section = self.patients[patient_id]["section"]

        return [
            self._result(record, 1.0)
            for record in self.records
            if record["section"] == section
            and record["visibility"] == config.CLINICAL
        ]

    def patient_card(self, patient_id, role):
        """Structured fields of one record for the clinician lookup page."""
        if config.CLINICAL not in config.ROLE_VISIBILITY.get(role, set()):
            return None

        resolved = self.resolve_patient(patient_id)

        if resolved is None:
            return None

        info = self.patients[resolved]
        chunk_ids = [r["id"] for r in self.lookup_patient(resolved, role)]

        return {"patient_id": resolved, **info["fields"], "chunk_ids": chunk_ids}

    @staticmethod
    def _parse_fields(text):
        fields = {}
        for label, value in PATIENT_FIELD.findall(text):
            fields[FIELD_KEYS[label]] = " ".join(value.split())
        return fields

    # ------------------------------------------------
    # HELPERS
    # ------------------------------------------------

    @staticmethod
    def _result(record, score):
        return {
            "id": record["id"],
            "chunk": record["text"],
            "visibility": record["visibility"],
            "section": record["section"],
            "score": round(score, 4),
        }

    def chunks_with_visibility(self, visibility):
        """All chunk texts carrying the given visibility label (for tests)."""
        self._ensure_loaded()
        return [r["text"] for r in self.records if r["visibility"] == visibility]


if __name__ == "__main__":
    rag = RAGPipeline()
    rag.build_index()

    checks = [
        ("What is the cancellation fee?", config.FRONT_DESK),
        ("What is Daniel Koh's diagnosis?", config.FRONT_DESK),
        ("What is Daniel Koh's diagnosis?", config.CLINICIAN),
    ]

    for question, role in checks:
        print(f"\n=== {role}: {question}")
        for result in rag.search(question, role=role):
            print(f"  {result['score']:.3f}  {result['id']}")

    print("\n=== clinician lookup P-1003")
    for result in rag.lookup_patient("P-1003", config.CLINICIAN):
        print(f"  {result['id']}")
