"use client";

import { useChat } from "@/hooks/useChat";
import { ChatWindow } from "@/components/ChatWindow";
import { ChatInput } from "@/components/ChatInput";
import { Sidebar } from "@/components/Sidebar";

export default function ChatPage() {
  const { messages, sendMessage } = useChat();

  return (
    <div className="flex h-screen bg-[#0f0f0f] text-white">
      <Sidebar />
      <div className="flex flex-col flex-1">
        <ChatWindow messages={messages} />
        <ChatInput onSend={sendMessage} />
      </div>
    </div>
  );
}
