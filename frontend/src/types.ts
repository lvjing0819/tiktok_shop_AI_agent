export type Api = (
  path: string,
  body?: unknown,
  method?: string,
) => Promise<any>;
export type Session = {
  id: string;
  title: string;
  summary: string;
  messages?: { role: string; content: string }[];
};
export type Run = {
  id: string;
  revision: number;
  question: string;
  status: string;
  created_at: number;
  progress?: { completed: number; total: number };
  plan: {
    title: string;
    steps: {
      id: string;
      agent: string;
      goal: string;
      tool: string;
      arguments: Record<string, unknown>;
      bindings: Record<string, unknown>;
      depends_on: string[];
      success_criteria: string;
    }[];
    clarification?: string;
  };
  steps?: Record<
    string,
    {
      status: string;
      error?: string;
      result?: {
        summary: string;
        agent_summary?: string;
        data?: any;
        evidence?: any[];
      };
    }
  >;
  final_delivery?: {
    answer: string;
    limitations: string[];
    source_steps: string[];
  };
  usage: Record<string, number>;
  pending_approval?: unknown;
  events?: any[];
};
export type Preferences = {
  market: string;
  brand_style: string;
  forbidden_claims: string;
  notes: string;
  revision: number;
};
