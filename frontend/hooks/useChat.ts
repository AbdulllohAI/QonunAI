import { useState } from "react";

export function useChat() {
  const [messages, setMessages] = useState<any[]>([]);

  async function sendMessage(content: string) {
    const newMessages = [...messages, { role: "user", content }];
    setMessages(newMessages);

    const res = await fetch("/api/stream", {
      method: "POST",
      body: JSON.stringify({ messages: newMessages }),
    });

    const reader = res.body!.getReader();
    let assistantMessage = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      assistantMessage += new TextDecoder().decode(value);

      setMessages([
        ...newMessages,
        { role: "assistant", content: assistantMessage },
      ]);
    }
  }

  return { messages, sendMessage };
}
