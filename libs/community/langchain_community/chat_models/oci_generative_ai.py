import json
import re
import uuid
from abc import ABC, abstractmethod
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Type,
    Union,
)

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.chat_models import (
    BaseChatModel,
    generate_from_stream,
)
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    ChatMessage,
    HumanMessage,
    SystemMessage,
    ToolCall,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.pydantic_v1 import BaseModel, Extra
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_function
from langchain_core.output_parsers.base import OutputParserLike
from langchain_core.output_parsers.openai_tools import (
    JsonOutputKeyToolsParser,
    PydanticToolsParser,
)

from langchain_community.llms.oci_generative_ai import OCIGenAIBase
from langchain_community.llms.utils import enforce_stop_tokens

CUSTOM_ENDPOINT_PREFIX = "ocid1.generativeaiendpoint"

JSON_TO_PYTHON_TYPES = {
    "string": "str",
    "number": "float",
    "boolean": "bool",
    "integer": "int",
    "array": "List",
    "object": "Dict",
}


def _remove_signature_from_tool_description(name: str, description: str) -> str:
    """
    Removes the `{name}{signature} - ` prefix and Args: section from tool description.
    The signature is usually present for tools created with the @tool decorator,
    whereas the Args: section may be present in function doc blocks.
    """
    description = re.sub(rf"^{name}\(.*?\) -(?:> \w+? -)? ", "", description)
    description = re.sub(r"(?s)(?:\n?\n\s*?)?Args:.*$", "", description)
    return description


def _format_oci_tool_calls(
    tool_calls: Optional[List[Any]] = None,
) -> List[Dict]:
    """
    Formats a OCI GenAI API response into the tool call format used elsewhere in Langchain.
    """
    if not tool_calls:
        return []

    formatted_tool_calls = []
    for tool_call in tool_calls:
        formatted_tool_calls.append(
            {
                "id": uuid.uuid4().hex[:],
                "function": {
                    "name": tool_call.name,
                    "arguments": json.dumps(tool_call.parameters),
                },
                "type": "function",
            }
        )
    return formatted_tool_calls


def _convert_oci_tool_call_to_langchain(tool_call: Any) -> ToolCall:
    """Convert a Cohere tool call into langchain_core.messages.ToolCall"""
    _id = uuid.uuid4().hex[:]
    return ToolCall(name=tool_call.name, args=tool_call.parameters, id=_id)


class Provider(ABC):
    @property
    @abstractmethod
    def stop_sequence_key(self) -> str: ...

    @abstractmethod
    def chat_response_to_text(self, response: Any) -> str: ...

    @abstractmethod
    def chat_stream_to_text(self, event_data: Dict) -> str: ...

    @abstractmethod
    def chat_generation_info(self, response: Any) -> Dict[str, Any]: ...

    @abstractmethod
    def get_role(self, message: BaseMessage) -> str: ...

    @abstractmethod
    def messages_to_oci_params(self, messages: Any, **kwargs: Any) -> Dict[str, Any]:
        ...

    @abstractmethod
    def convert_to_oci_tool(
        self,
        tool: Union[Union[Dict[str, Any], Type[BaseModel], Callable, BaseTool]],
    ) -> Dict[str, Any]:
        ...


class CohereProvider(Provider):
    stop_sequence_key = "stop_sequences"

    def __init__(self) -> None:
        from oci.generative_ai_inference import models

        self.oci_chat_request = models.CohereChatRequest
        self.oci_tool = models.CohereTool
        self.oci_tool_param = models.CohereParameterDefinition
        self.oci_tool_result = models.CohereToolResult
        self.oci_tool_call = models.CohereToolCall
        self.oci_chat_message = {
            "USER": models.CohereUserMessage,
            "CHATBOT": models.CohereChatBotMessage,
            "SYSTEM": models.CohereSystemMessage,
            "TOOL": models.CohereToolMessage,
        }
        self.chat_api_format = models.BaseChatRequest.API_FORMAT_COHERE

    def chat_response_to_text(self, response: Any) -> str:
        return response.data.chat_response.text

    def chat_stream_to_text(self, event_data: Dict) -> str:
        if "text" in event_data and "finishReason" not in event_data:
            return event_data["text"]
        else:
            return ""

    def chat_generation_info(self, response: Any) -> Dict[str, Any]:
        generation_info: Dict[str, Any] = {
            "documents": response.data.chat_response.documents,
            "citations": response.data.chat_response.citations,
            "search_queries": response.data.chat_response.search_queries,
            "is_search_required": response.data.chat_response.is_search_required,
            "finish_reason": response.data.chat_response.finish_reason,
        }
        if response.data.chat_response.tool_calls:
            # Only populate tool_calls when 1) present on the response and
            #  2) has one or more calls.
            generation_info["tool_calls"] = _format_oci_tool_calls(
                response.data.chat_response.tool_calls
            )

        return generation_info

    def get_role(self, message: BaseMessage) -> str:
        if isinstance(message, HumanMessage):
            return "USER"
        elif isinstance(message, AIMessage):
            return "CHATBOT"
        elif isinstance(message, SystemMessage):
            return "SYSTEM"
        elif isinstance(message, ToolMessage):
            return "TOOL"
        else:
            raise ValueError(f"Got unknown type {message}")

    def messages_to_oci_params(
        self, messages: Sequence[ChatMessage], **kwargs: Any
    ) -> Dict[str, Any]:
        is_force_single_step = kwargs.get("is_force_single_step") or False

        oci_chat_history = []

        for msg in messages[:-1]:
            if self.get_role(msg) == "USER" or self.get_role(msg) == "SYSTEM":
                oci_chat_history.append(
                    self.oci_chat_message[self.get_role(msg)](message=msg.content)
                )
            elif self.get_role(msg) == "CHATBOT":
                if msg.tool_calls and is_force_single_step:
                    continue
                tool_calls = (
                    [
                        self.oci_tool_call(name=tc["name"], parameters=tc["args"])
                        for tc in msg.tool_calls
                    ]
                    if msg.tool_calls
                    else None
                )
                # TODO: report bug when message content is empty
                msg_content = msg.content if msg.content else " "
                oci_chat_history.append(
                    self.oci_chat_message[self.get_role(msg)](
                        message=msg_content, tool_calls=tool_calls
                    )
                )

        # Get the messages for the current chat turn
        current_chat_turn_messages = []
        for message in messages[::-1]:
            current_chat_turn_messages.append(message)
            if isinstance(message, HumanMessage):
                break
        current_chat_turn_messages = current_chat_turn_messages[::-1]

        oci_tool_results = []
        for message in current_chat_turn_messages:
            if isinstance(message, ToolMessage):
                tool_message = message
                previous_ai_msgs = [
                    message
                    for message in current_chat_turn_messages
                    if isinstance(message, AIMessage) and message.tool_calls
                ]
                if previous_ai_msgs:
                    previous_ai_msg = previous_ai_msgs[-1]
                    for lc_tool_call in previous_ai_msg.tool_calls:
                        if lc_tool_call["id"] == tool_message.tool_call_id:
                            tool_result = self.oci_tool_result()
                            tool_result.call = self.oci_tool_call(
                                name=lc_tool_call["name"],
                                parameters=lc_tool_call["args"],
                            )
                            tool_result.outputs = [
                                {"output": tool_message.content}
                            ]  # use function convert_to_documents
                            oci_tool_results.append(tool_result)

        if not oci_tool_results:
            oci_tool_results = None

        message_str = "" if oci_tool_results else messages[-1].content

        oci_params = {
            "message": message_str,
            "chat_history": oci_chat_history,
            "tool_results": oci_tool_results,
            "api_format": self.chat_api_format,
        }

        return {k: v for k, v in oci_params.items() if v is not None}

    def convert_to_oci_tool(
        self,
        tool: Union[Union[Dict[str, Any], Type[BaseModel], Callable, BaseTool]],
    ) -> Dict[str, Any]:
        """
        Convert a BaseTool instance, JSON schema dict, or BaseModel type to a OCI tool.
        """
        if isinstance(tool, BaseTool):
            return self.oci_tool(
                name=tool.name,
                description=_remove_signature_from_tool_description(
                    tool.name, tool.description
                ),
                parameter_definitions={
                    p_name: self.oci_tool_param(
                        description=p_def.get("description")
                        if "description" in p_def
                        else "",
                        type=JSON_TO_PYTHON_TYPES.get(
                            p_def.get("type"), p_def.get("type")
                        ),
                        is_required="default" not in p_def,
                    )
                    for p_name, p_def in tool.args.items()
                },
            )
        elif isinstance(tool, dict):
            if not all(k in tool for k in ("title", "description", "properties")):
                raise ValueError(
                    "Unsupported dict type. Tool must be passed in as a BaseTool instance, JSON schema dict, or BaseModel type."  # noqa: E501
                )
            return self.oci_tool(
                name=tool.get("title"),
                description=tool.get("description"),
                parameter_definitions={
                    p_name: self.oci_tool_param(
                        description=p_def.get("description"),
                        type=JSON_TO_PYTHON_TYPES.get(
                            p_def.get("type"), p_def.get("type")
                        ),
                        is_required="default" not in p_def,
                    )
                    for p_name, p_def in tool.get("properties", {}).items()
                },
            )
        elif (isinstance(tool, type) and issubclass(tool, BaseModel)) or callable(tool):
            as_json_schema_function = convert_to_openai_function(tool)
            parameters = as_json_schema_function.get("parameters", {})
            properties = parameters.get("properties", {})
            return self.oci_tool(
                name=as_json_schema_function.get("name"),
                description=as_json_schema_function.get(
                    "description",
                    as_json_schema_function.get("name"),
                ),
                parameter_definitions={
                    p_name: self.oci_tool_param(
                        description=p_def.get("description"),
                        type=JSON_TO_PYTHON_TYPES.get(
                            p_def.get("type"), p_def.get("type")
                        ),
                        is_required=p_name in parameters.get("required", []),
                    )
                    for p_name, p_def in properties.items()
                },
            )
        else:
            raise ValueError(
                f"Unsupported tool type {type(tool)}. Tool must be passed in as a BaseTool instance, JSON schema dict, or BaseModel type."  # noqa: E501
            )


class MetaProvider(Provider):
    stop_sequence_key = "stop"

    def __init__(self) -> None:
        from oci.generative_ai_inference import models

        self.oci_chat_request = models.GenericChatRequest
        self.oci_chat_message = {
            "USER": models.UserMessage,
            "SYSTEM": models.SystemMessage,
            "ASSISTANT": models.AssistantMessage,
        }
        self.oci_chat_message_content = models.TextContent
        self.chat_api_format = models.BaseChatRequest.API_FORMAT_GENERIC

    def chat_response_to_text(self, response: Any) -> str:
        return response.data.chat_response.choices[0].message.content[0].text

    def chat_stream_to_text(self, event_data: Dict) -> str:
        if "message" in event_data:
            return event_data["message"]["content"][0]["text"]
        else:
            return ""

    def chat_generation_info(self, response: Any) -> Dict[str, Any]:
        return {
            "finish_reason": response.data.chat_response.choices[0].finish_reason,
            "time_created": str(response.data.chat_response.time_created),
        }

    def get_role(self, message: BaseMessage) -> str:
        # meta only supports alternating user/assistant roles
        if isinstance(message, HumanMessage):
            return "USER"
        elif isinstance(message, AIMessage):
            return "ASSISTANT"
        elif isinstance(message, SystemMessage):
            return "SYSTEM"
        else:
            raise ValueError(f"Got unknown type {message}")

    def messages_to_oci_params(
        self, messages: List[BaseMessage], **kwargs: Any
    ) -> Dict[str, Any]:
        oci_messages = [
            self.oci_chat_message[self.get_role(msg)](
                content=[self.oci_chat_message_content(text=msg.content)]
            )
            for msg in messages
        ]
        oci_params = {
            "messages": oci_messages,
            "api_format": self.chat_api_format,
            "top_k": -1,
        }

        return oci_params

    def convert_to_oci_tool(
        self,
        tool: Union[Union[Dict[str, Any], Type[BaseModel], Callable, BaseTool]],
    ) -> Dict[str, Any]:
        raise NotImplementedError("Tools not supported for Meta models")


class ChatOCIGenAI(BaseChatModel, OCIGenAIBase):
    """ChatOCIGenAI chat model integration.

    Setup:
      Install ``langchain-community`` and the ``oci`` sdk.

      .. code-block:: bash

          pip install -U langchain-community oci

    Key init args — completion params:
        model_id: str
            Id of the OCIGenAI chat model to use, e.g., cohere.command-r-16k.
        is_stream: bool
            Whether to stream back partial progress
        model_kwargs: Optional[Dict]
            Keyword arguments to pass to the specific model used, e.g., temperature, max_tokens.

    Key init args — client params:
        service_endpoint: str
            The endpoint URL for the OCIGenAI service, e.g., https://inference.generativeai.us-chicago-1.oci.oraclecloud.com.
        compartment_id: str
            The compartment OCID.
        auth_type: str
            The authentication type to use, e.g., API_KEY (default), SECURITY_TOKEN, INSTANCE_PRINCIPAL, RESOURCE_PRINCIPAL.
        auth_profile: Optional[str]
            The name of the profile in ~/.oci/config, if not specified , DEFAULT will be used.
        provider: str
            Provider name of the model. Default to None, will try to be derived from the model_id otherwise, requires user input.
    See full list of supported init args and their descriptions in the params section.

    Instantiate:
        .. code-block:: python

            from langchain_community.chat_models import ChatOCIGenAI

            chat = ChatOCIGenAI(
                model_id="cohere.command-r-16k",
                service_endpoint="https://inference.generativeai.us-chicago-1.oci.oraclecloud.com",
                compartment_id="MY_OCID",
                model_kwargs={"temperature": 0.7, "max_tokens": 500},
            )

    Invoke:
        .. code-block:: python
            messages = [
                SystemMessage(content="your are an AI assistant."),
                AIMessage(content="Hi there human!"),
                HumanMessage(content="tell me a joke."),
            ]
            response = chat.invoke(messages)

    Stream:
        .. code-block:: python

        for r in chat.stream(messages):
            print(r.content, end="", flush=True)

    Response metadata
        .. code-block:: python

        response = chat.invoke(messages)
        print(response.response_metadata)

    """  # noqa: E501

    class Config:
        """Configuration for this pydantic object."""

        extra = Extra.forbid

    @property
    def _llm_type(self) -> str:
        """Return type of llm."""
        return "oci_generative_ai_chat"

    @property
    def _provider_map(self) -> Mapping[str, Any]:
        """Get the provider map"""
        return {
            "cohere": CohereProvider(),
            "meta": MetaProvider(),
        }

    @property
    def _provider(self) -> Any:
        """Get the internal provider object"""
        return self._get_provider(provider_map=self._provider_map)

    def _prepare_request(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]],
        stream: bool,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        try:
            from oci.generative_ai_inference import models

        except ImportError as ex:
            raise ModuleNotFoundError(
                "Could not import oci python package. "
                "Please make sure you have the oci package installed."
            ) from ex

        oci_params = self._provider.messages_to_oci_params(messages, **kwargs)

        oci_params["is_force_single_step"] = kwargs.get("is_force_single_step") or False

        oci_params["is_stream"] = stream  # self.is_stream
        _model_kwargs = self.model_kwargs or {}

        if stop is not None:
            _model_kwargs[self._provider.stop_sequence_key] = stop

        chat_params = {**_model_kwargs, **kwargs, **oci_params}

        if self.model_id.startswith(CUSTOM_ENDPOINT_PREFIX):
            serving_mode = models.DedicatedServingMode(endpoint_id=self.model_id)
        else:
            serving_mode = models.OnDemandServingMode(model_id=self.model_id)

        request = models.ChatDetails(
            compartment_id=self.compartment_id,
            serving_mode=serving_mode,
            chat_request=self._provider.oci_chat_request(**chat_params),
        )

        return request

    def bind_tools(
        self,
        tools: Sequence[Union[Dict[str, Any], Type[BaseModel], Callable, BaseTool]],
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, BaseMessage]:
        formatted_tools = [self._provider.convert_to_oci_tool(tool) for tool in tools]
        return super().bind(tools=formatted_tools, **kwargs)

    def with_structured_output(
        self,
        schema: Union[Dict, Type[BaseModel]],
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, Union[Dict, BaseModel]]:
        """Model wrapper that returns outputs formatted to match the given schema.

        Args:
            schema: The output schema as a dict or a Pydantic class. If a Pydantic class
                then the model output will be an object of that class. If a dict then
                the model output will be a dict.

        Returns:
            A Runnable that takes any ChatModel input and returns either a dict or
            Pydantic class as output.
        """
        is_pydantic_schema = isinstance(schema, type) and issubclass(schema, BaseModel)
        llm = self.bind_tools([schema], **kwargs)
        if is_pydantic_schema:
            output_parser: OutputParserLike = PydanticToolsParser(
                tools=[schema], first_tool_only=True
            )
        else:
            key_name = self._provider.convert_to_oci_tool(schema)["name"]
            output_parser = JsonOutputKeyToolsParser(
                key_name=key_name, first_tool_only=True
            )

        return llm | output_parser

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Call out to a OCIGenAI chat model.

        Args:
            messages: list of LangChain messages
            stop: Optional list of stop words to use.

        Returns:
            LangChain ChatResult

        Example:
            .. code-block:: python

               messages = [
                            HumanMessage(content="hello!"),
                            AIMessage(content="Hi there human!"),
                            HumanMessage(content="Meow!")
                          ]

               response = llm.invoke(messages)
        """
        if self.is_stream:
            stream_iter = self._stream(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )
            return generate_from_stream(stream_iter)

        request = self._prepare_request(messages, stop=stop, stream=False, **kwargs)
        response = self.client.chat(request)

        content = self._provider.chat_response_to_text(response)

        if stop is not None:
            content = enforce_stop_tokens(content, stop)

        generation_info = self._provider.chat_generation_info(response)

        llm_output = {
            "model_id": response.data.model_id,
            "model_version": response.data.model_version,
            "request_id": response.request_id,
            "content-length": response.headers["content-length"],
        }

        if "tool_calls" in generation_info:
            tool_calls = [
                _convert_oci_tool_call_to_langchain(tool_call)
                for tool_call in response.data.chat_response.tool_calls
            ]
        else:
            tool_calls = []

        message = AIMessage(
            content=content,
            additional_kwargs=generation_info,
            tool_calls=tool_calls,
        )
        return ChatResult(
            generations=[
                ChatGeneration(message=message, generation_info=generation_info)
            ],
            llm_output=llm_output,
        )

    def _stream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        request = self._prepare_request(messages, stop=stop, stream=True, **kwargs)
        response = self.client.chat(request)

        for event in response.data.events():
            delta = self._provider.chat_stream_to_text(json.loads(event.data))
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=delta))
            if run_manager:
                run_manager.on_llm_new_token(delta, chunk=chunk)
            yield chunk
