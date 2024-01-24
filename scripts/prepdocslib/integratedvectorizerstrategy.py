from typing import Optional

# Workaround to use the preview SDK
from azure.search.documents.indexes._generated.models import (
    IndexProjectionMode,
    SearchIndexerIndexProjections,
    SearchIndexerIndexProjectionSelector,
    SearchIndexerIndexProjectionsParameters,
    SearchIndexerSkillset,
)
from azure.search.documents.indexes.models import (
    AzureOpenAIEmbeddingSkill,
    AzureOpenAIParameters,
    AzureOpenAIVectorizer,
    FieldMapping,
    InputFieldMappingEntry,
    OutputFieldMappingEntry,
    SearchIndexer,
    SearchIndexerDataContainer,
    SearchIndexerDataSourceConnection,
    SplitSkill,
)

from .blobmanager import BlobManager
from .embeddings import AzureOpenAIEmbeddingService
from .listfilestrategy import ListFileStrategy
from .searchmanager import SearchManager
from .strategy import DocumentAction, SearchInfo, Strategy


class IntegratedVectorizerStrategy(Strategy):
    """
    Strategy for ingesting and vectorizing documents into a search service from files stored storage account
    """

    def __init__(
        self,
        list_file_strategy: ListFileStrategy,
        blob_manager: BlobManager,
        embeddings: Optional[AzureOpenAIEmbeddingService],
        subscriptionId: str,
        searchServiceUserAssginedId: str,
        document_action: DocumentAction = DocumentAction.Add,
        search_analyzer_name: Optional[str] = None,
        use_acls: bool = False,
        category: Optional[str] = None,
    ):
        self.list_file_strategy = list_file_strategy
        self.blob_manager = blob_manager
        self.document_action = document_action
        self.embeddings = embeddings
        self.subscriptionId = subscriptionId
        self.search_user_assigned_identity = searchServiceUserAssginedId
        self.search_analyzer_name = search_analyzer_name
        self.use_acls = use_acls
        self.category = category

    async def createEmbeddingSkill(self, index_name: str):
        skillset_name = f"{index_name}-skillset"

        split_skill = SplitSkill(
            description="Split skill to chunk documents",
            text_split_mode="pages",
            context="/document",
            maximum_page_length=2048,
            page_overlap_length=20,
            inputs=[
                InputFieldMappingEntry(name="text", source="/document/content"),
            ],
            outputs=[OutputFieldMappingEntry(name="textItems", target_name="pages")],
        )

        if self.embeddings is None:
            raise ValueError("Expecting Azure Open AI instance")

        embedding_skill = AzureOpenAIEmbeddingSkill(
            description="Skill to generate embeddings via Azure OpenAI",
            context="/document/pages/*",
            resource_uri=f"https://{self.embeddings.open_ai_service}.openai.azure.com",
            deployment_id=self.embeddings.open_ai_deployment,
            auth_identity=None,
            inputs=[
                InputFieldMappingEntry(name="text", source="/document/pages/*"),
            ],
            outputs=[OutputFieldMappingEntry(name="embedding", target_name="vector")],
        )

        index_projections = SearchIndexerIndexProjections(
            selectors=[
                SearchIndexerIndexProjectionSelector(
                    target_index_name=index_name,
                    parent_key_field_name="parent_id",
                    source_context="/document/pages/*",
                    mappings=[
                        InputFieldMappingEntry(name="content", source="/document/pages/*"),
                        InputFieldMappingEntry(name="embedding", source="/document/pages/*/vector"),
                        InputFieldMappingEntry(name="sourcepage", source="/document/metadata_storage_name"),
                    ],
                ),
            ],
            parameters=SearchIndexerIndexProjectionsParameters(
                projection_mode=IndexProjectionMode.SKIP_INDEXING_PARENT_DOCUMENTS
            ),
        )

        skillset = SearchIndexerSkillset(
            name=skillset_name,
            description="Skillset to chunk documents and generating embeddings",
            skills=[split_skill, embedding_skill],
            index_projections=index_projections,
        )

        return skillset

    async def setup(self, search_info: SearchInfo):
        search_manager = SearchManager(
            search_info,
            self.search_analyzer_name,
            self.use_acls,
            True,
            self.embeddings,
            search_images=False,
        )

        if self.embeddings is None:
            raise ValueError("Expecting Azure Open AI instance")

        await search_manager.create_index(
            [
                AzureOpenAIVectorizer(
                    name="myOpenAI",
                    kind="azureOpenAI",
                    azure_open_ai_parameters=AzureOpenAIParameters(
                        resource_uri=f"https://{self.embeddings.open_ai_service}.openai.azure.com",
                        deployment_id=self.embeddings.open_ai_deployment,
                        auth_identity=None,
                    ),
                ),
            ]
        )

        # create indexer client
        ds_client = search_info.create_search_indexer_client()
        ds_container = SearchIndexerDataContainer(name=self.blob_manager.container)
        data_source_connection = SearchIndexerDataSourceConnection(
            name=f"{search_info.index_name}-blob",
            type="azureblob",
            connection_string=self.blob_manager.get_managedidentity_connectionstring(),
            container=ds_container,
        )

        await ds_client.create_or_update_data_source_connection(data_source_connection)
        print("Search indexer data source connection updated.")

        embedding_skillset = await self.createEmbeddingSkill(search_info.index_name)
        await ds_client.create_or_update_skillset(embedding_skillset)

    async def run(self, search_info: SearchInfo):
        if self.document_action == DocumentAction.Add:
            files = self.list_file_strategy.list()
            async for file in files:
                try:
                    await self.blob_manager.upload_blob(file)
                finally:
                    if file:
                        file.close()

        # Create an indexer
        indexer_name = f"{search_info.index_name}-indexer"

        indexer = SearchIndexer(
            name=indexer_name,
            description="Indexer to index documents and generate embeddings",
            skillset_name=f"{search_info.index_name}-skillset",
            target_index_name=search_info.index_name,
            data_source_name=f"{search_info.index_name}-blob",
            # Map the metadata_storage_name field to the title field in the index to display the PDF title in the search results
            field_mappings=[FieldMapping(source_field_name="metadata_storage_name", target_field_name="title")],
        )

        indexer_client = search_info.create_search_indexer_client()
        indexer_result = await indexer_client.create_or_update_indexer(indexer)

        # Run the indexer
        await indexer_client.run_indexer(indexer_name)

        print(
            f"Successfully created index, indexer: {indexer_result.name} and skillset. Please navigate to search service in azure portal to view the status of the indexer."
        )
