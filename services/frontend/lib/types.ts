export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  image_base64?: string;
}

export interface ChatResponse {
  response: string;
  prediction_id?: string | null;
  annotated_image?: string | null;
  agent_loop_time_s?: number | null;
  iterations?: number | null;
  tools_called?: string[];
  context_limit_exceeded?: boolean;
}