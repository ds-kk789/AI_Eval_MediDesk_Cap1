"""Chunk knowledge.txt, embed locally (MiniLM), index in FAISS, search by role.

Port from: AI_Eval_Module_4_3_Steps/backend/rag_pipeline.py

TODO:
- Parse [VISIBILITY: PUBLIC|CLINICAL] markers; every chunk keeps its label
- RecursiveCharacterTextSplitter(CHUNK_SIZE=500 chars, CHUNK_OVERLAP=100)
- search(question, role): over-fetch TOP_K * OVERFETCH, drop chunks the role may not see (BR-5)
- Return similarity scores too; drift_check.py uses top-1 similarity
"""
