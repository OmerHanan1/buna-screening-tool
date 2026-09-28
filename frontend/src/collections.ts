export interface CollectionPaper { id: string; title: string; version: string }
export interface CollectionEntry {
  reference_id: number; title: string; status: string; reason: string; comparison_ready: boolean;
  version: string; license: string; access_basis: string; paper_id: string | null;
}
export interface LibraryCollection {
  id: string; name: string; ready_papers: CollectionPaper[]; ready_unique: number;
  ready_references: number; total_references: number; entries: CollectionEntry[];
}
export interface CollectionList { collections: LibraryCollection[]; default_collection_id: string | null }
