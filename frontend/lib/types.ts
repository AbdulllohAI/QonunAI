export type Language = "uz-Latn" | "uz-Cyrl" | "ru" | "en";
export type Mode = "qa" | "document_analysis" | "law_search" | "by_article";
export type RiskLevel = "low" | "medium" | "high";

export interface Citation {
  tag: string;
  chunk_id: string;
  act_id: string;
  law_name: string;
  article_number: string | null;
  act_type: string;
  citation: string;
  language: string;
  hierarchy_path: string;
  date_of_adoption: string | null;
  last_updated: string | null;
  source_url: string | null;
  score: number;
  supporting: boolean;
}

export interface Risk {
  level: RiskLevel;
  factors: string[];
  model_stated: string | null;
}

export interface ConflictNote {
  higher: string;
  lower: string;
  rule: string;
  explanation: string;
}

export interface HierarchyInfo {
  controlling: string | null;
  conflicts: ConflictNote[];
}

export interface ValidationInfo {
  used_tags: string[];
  invalid_tags: string[];
  unverified_articles: string[];
  has_citations: boolean;
  clean: boolean;
}

export interface LegalAnswer {
  answer: string;
  citations: Citation[];
  risk: Risk;
  hierarchy: HierarchyInfo;
  validation: ValidationInfo;
  warning: string | null;
  disclaimer: string;
  language: string;
  mode: string;
  provider: string | null;
  model: string | null;
  timings: { retrieval_ms: number; llm_ms: number; total_ms: number };
  usage: { tokens_in: number; tokens_out: number };
  answered: boolean;
  refusal_reason: string | null;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations?: Citation[];
  risk?: Risk;
  hierarchy?: HierarchyInfo;
  validation?: ValidationInfo;
  warning?: string | null;
  disclaimer?: string;
  streaming?: boolean;
  error?: string;
  timings?: LegalAnswer["timings"];
}

export interface SearchHit {
  chunk_id: string;
  act_id: string;
  citation: string;
  law_name: string;
  article_number: string | null;
  act_type: string;
  language: string;
  hierarchy_path: string;
  heading: string | null;
  text: string;
  score: number;
  dense_score: number;
  sparse_score: number;
  rerank_score: number | null;
  source_url: string | null;
  via_crossref_from: string | null;
}

export interface SearchResponse {
  query: string;
  detected_language: string;
  hits: SearchHit[];
  dense_hits: number;
  sparse_hits: number;
  crossref_hits: number;
  took_ms: number;
}

export interface DocumentAnalysisResult extends LegalAnswer {
  document_id: string;
  filename: string;
  document: {
    detected_language: string;
    clause_count: number;
    text_length: number;
    truncated: boolean;
    probed_topics: string[];
    heuristic_flags: { code: string; message: string; excerpt: string }[];
  };
}

export interface LegalAct {
  id: string;
  act_type: string;
  status: string;
  short_name: string | null;
  title_uz: string | null;
  title_ru: string | null;
  title_en: string | null;
  doc_number: string | null;
  date_of_adoption: string | null;
  last_updated: string | null;
  issuing_body: string | null;
  source_url: string | null;
}
