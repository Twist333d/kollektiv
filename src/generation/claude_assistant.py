import json

from src.utils.logger import setup_logger
from src.utils.config import ANTHROPIC_API_KEY
from src.utils.decorators import error_handler
from typing import Dict, Any, List
from src.generation.tool_definitions import ToolManager, tool_manager
from src.generation.query_generator import QueryGenerator

import anthropic

logger = setup_logger('claude_assistant', "claude_assistant.log")

# Client Class
class ClaudeAssistant:
    def __init__(self, api_key :str =ANTHROPIC_API_KEY, model_name:str ="claude-3-5-sonnet-20240620"):
        self.client = None
        self.api_key = api_key
        self.model_name = model_name
        self.logger = logger
        self.base_system_prompt = """
                You are an advanced AI assistant with access to various tools, including a powerful RAG (Retrieval Augmented Generation) system. Your primary function is to provide accurate, relevant, and helpful information to users by leveraging your broad knowledge base and the specific information available through the RAG tool.

                Key guidelines:
                1. Use the RAG tool when queries likely require information from loaded documents or recent data not in your training.
                2. Analyze the user's question and conversation context before deciding to use the RAG tool.
                3. When using RAG, formulate precise queries to retrieve the most relevant information.
                4. Seamlessly integrate retrieved information into your responses, citing sources when appropriate.
                5. If the RAG tool doesn't provide relevant information, rely on your general knowledge.
                6. Always strive for accuracy, clarity, and helpfulness in your responses.
                7. Be transparent about the source of your information (general knowledge vs. RAG-retrieved data).
                8. If you're unsure about information or if it's not in the loaded documents, say so honestly.

                Do not:
                - Invent or hallucinate information not present in your knowledge base or the RAG-retrieved data.
                - Use the RAG tool for general knowledge questions that don't require specific document retrieval.
                - Disclose sensitive details about the RAG system's implementation or the document loading process.

                Currently loaded document summaries:
                {document_summaries}

                Remember to use your tools judiciously and always prioritize providing the most accurate and helpful information to the user.
                """
        self.system_prompt = self.base_system_prompt.format(document_summaries="No documents loaded yet.")
        self.conversation_history: List[Dict[str, str]] = []
        self.tool_manager = tool_manager  # Use the pre-initialized tool_manager
        self.tools: List[Dict[str, Any]] = []  # Initialize as an empty list
        self.retriever = None
        self.query_generator = QueryGenerator()

        self._init()


    @error_handler(logger)
    def _init(self):
        self.client = anthropic.Anthropic(api_key=self.api_key)
        self.tools = self.tool_manager.get_all_tools()  # Get all tools as a list of dicts
        self.logger.info("Successfully initialized Anthropic client")

    @error_handler(logger)
    def update_system_prompt(self, document_summaries: List[Dict[str, Any]]):
        summaries_text = "\n".join(
            f"- Document URL: {summary['source_url']}):\n Summary: {summary['summary']}"
            for summary in document_summaries
        )
        self.system_prompt = self.base_system_prompt.format(document_summaries=summaries_text)
        self.logger.info(f"Updated system prompt: {self.system_prompt}")

    @error_handler(logger)
    def generate_response(self, user_input: str) -> str:

        messages = self.conversation_history + [{"role": "user", "content": user_input}]

        response = self.client.messages.create(
            model=self.model_name,
            max_tokens=8192,
            system=self.system_prompt,
            messages=messages,
            tools=self.tool_manager.get_all_tools()
        )

        # check for tool use
        if response.stop_reason == "tool_use":
            for content in response.content:
                if content.type == 'tool_use' and content.name == 'rag_search':
                    # Formulate the best possible query using recent context
                    tool_result = self.call_tool(content.name, content.input, user_input)
                    messages.append({
                        'role': 'user',
                        'content': [{
                            'type': 'tool_result',
                            'tool_use_id': content.id,
                            'content': tool_result
                        }]
                    })

            final_response = self.client.messages.create(
                model=self.model_name,
                max_tokens=8192,
                system=self.system_prompt,
                messages=messages,
            )
            assistant_response = final_response.content[0].text
            self.update_conversation_history(user_input, assistant_response)
        else:
            assistant_response = response.content[0].text
            self.update_conversation_history(user_input, assistant_response)
            return assistant_response

    @error_handler(logger)
    def formulate_rag_query(self, user_input: str, recent_context: List[Dict[str, str]]) -> str:
        context_prompt = "\n".join([f"{msg['role']}: {msg['content']}" for msg in recent_context])

        query_generation_prompt = f"""
        Based on the following conversation context and the user's latest input, formulate the best possible search 
        query for retrieving relevant information using RAG from a local vector database.
        
        Query requirements:
        - Do not include any other text in your response, except for the query.

        Recent conversation context:
        {context_prompt}

        User's latest input: {user_input}

        Formulated search query:
        """

        response = self.client.messages.create(
            model=self.model_name,
            max_tokens=150,
            system="You are world's best query formulator for a RAG system. You know how to properly formulate search "
                   "queries that are relevant to the user's inquiry and take into account the recent conversation context.",
            messages=[{"role": "user", "content": query_generation_prompt}]
        )

        return response.content[0].text.strip()

    @error_handler(logger)
    def get_recent_context(self, n_messages: int = 6) -> List[Dict[str, str]]:
        """Retrieves last 6 messages (3 user messages + 3 assistant messages)"""
        return self.conversation_history[-n_messages:]

    @error_handler(logger)
    def call_tool(self, tool_name: str,
                  tool_input: Dict[str, Any],
                  user_input: str) -> str:
        if tool_name == "rag_search":
            search_results = self.use_rag_search(user_input)
            return json.dumps(search_results)  # Convert results to a string for passing back to Claude
        else:
            raise ValueError(f"Tool {tool_name} not supported")

    @error_handler(logger)
    def use_rag_search(self, user_input: str) -> str:
        # Get recent conversation context (last 3 messages)
        recent_context = self.get_recent_context()

        # prepare queries for search
        rag_query = self.formulate_rag_query(user_input, recent_context)
        multiple_queries = self.query_generator.generate_multi_query(rag_query)
        combined_queries = self.query_generator.combine_queries(rag_query, multiple_queries)

        # get search ranked search results
        results = self.retriever.retrieve(rag_query, combined_queries)
        return json.dumps(results)


    @error_handler(logger)
    def update_conversation_history(self, user_input: str, assistant_response: str):
        self.conversation_history.append({"role": "user", "content": user_input})
        self.conversation_history.append({"role": "assistant", "content": assistant_response})

    @error_handler(logger)
    def generate_document_summary(self, chunks: List[Dict[str, Any]]) -> Dict[str, Any]:
        # Extract basic metadata from the first chunk
        first_chunk = chunks[0]
        source_url = first_chunk['metadata']['source_url']
        total_chunks = len(chunks)

        # Prepare content for summary generation
        content_sample = "\n\n".join(
            [chunk['data']['text'][:500] for chunk in chunks[:5]])  # First 500 chars of first 5 chunks

        summary_prompt = f"""
            Generate a concise summary of the following document. Include key topics or themes.
            Limit the summary to 150 words. Do not include any pre-ambule or boilerplate text.

            Document metadata:
            Source URL: {source_url}
            Total Chunks: {total_chunks}

            Content Sample:
            {content_sample}
            """

        response = self.client.messages.create(
            model=self.model_name,
            max_tokens=200,
            system="You are an AI assistant that is used in the RAG system. Your goal is to generate a concise "
                   "summary of the documents in the database, that are loaded into the database. You are perfect at "
                   "making concise summaries that capture the core meaning, topics, themes of the loaded documents.",
            messages=[{"role": "user", "content": summary_prompt}]
        )

        summary = response.content[0].text

        return {
            'source_url': source_url,
            'total_chunks': total_chunks,
            'summary': summary,
        }

    @error_handler(logger)
    def preprocess_ranked_documents(self, ranked_documents: Dict[str, Any]) -> List[str]:
        """
        Converts ranked documents into a structured string for passing to the Claude API.
        """
        preprocessed_context = []

        for _, result in ranked_documents.items(): # The first item (_) is the key, second (result) is the dictionary.
            relevance_score = result.get('relevance_score')
            text = result.get('text')

            # create a structured format
            formatted_document = (
                f"Document's relevance score: {relevance_score}: \n"
                f"Document text: {text}: \n"
                f"--------\n"
            )
            preprocessed_context.append(formatted_document)

            # self.logger.debug(f"Printing pre-chunks preprocessed_context {formatted_document}")

        return preprocessed_context

    @error_handler(logger)
    def get_augmented_response(self, user_query: str, context: Dict[str, Any], model_name: str =
    "claude-3-5-sonnet-20240620"):

        # process context
        preprocessed_context = self.preprocess_ranked_documents(context)

        system_prompt = (
            f"You are a knowledgable financial research assistant. Your users are inquiring about an annual report."
            f"You will be given context, extracted by an LLM that will help in answering the "
            f"questions. Each context has a relevance score and the document itself"
            f"If the provided context is not relevant, please inform the user that you can not "
            f"answer the question based on the provided information."
            f"If the provided context is relevant, answer the question based on the contex")

        messages = [
            {"role": "user", "content": f"Context: \n\n{preprocessed_context}\n\n."
                                    f"Answer the questions based on the provided context."
                                    f"Question: {user_query}"},

        ]

        try:
            response = self.client.messages.create(
                model=model_name,
                max_tokens=8192,
                system=system_prompt,
                messages=messages,
            )
            content = response.content[0].text
            self.logger.debug("Received response from Anthropic")
        except Exception as e:
            self.logger.error(f"Exception while processing Anthropic response: {e}")
            raise

        return content



def main():
    ca = ClaudeAssistant()

    user_input = input("What do you want to chat about? ")
    while user_input != "exit":
        response = ca.generate_response(user_input)
        print(response)

if __name__ == "__main__":
    main()