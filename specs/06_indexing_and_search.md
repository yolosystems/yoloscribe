## Indexing and Search

This document describes how indexing and search will work with YoloScribe.
All content in YoloScribe is stored in a content.md file and organized under a "site" which is owned by a specific user.
These content.md files need to be indexed so that they can be available via a search capability.

### Indexing

Indexing should be implemented the following way:
1. Whenever a content.md file is edited by an agent or via that chat interface, a job to index the new content.md file will be created and pushed onto an SQS queue. The payload should contain the S3 path to the content.md file that needs to be indexed.
2. There will be a new container called "indexer" that will poll the SQS queue waiting for indexing jobs. When a job is found, the poller will launch an indexer job into the K8S cluster. This should following the same design as the agent-runner, where the poller and the runner share the same codebase and container image, but the starting command is different. The poller creates instanes of K8S jobs when it dequeues the index message.
3. The index job will do the following:
    a. Retrieve the contents.md file in the SQS payload
    b. Chunk the file using an intelligent chunking strategy. Since these are markdown files, the chunking should break the file into the natural sections of the markdown. Suggest some libraries that may exist to do this or come up with a way to implement it.
    c. Chunks will be assigned a UUID and written to a prefix under the path containing the content.md file. For example, if the path to the content.md file is: /knuth/content.md, then the chunks will be written to /knuth/.chunks/[chunk_uuid]
    d. For each chunk we will create an embedding. If the strands-agents framework defines an SDK for creating embeddings, we can use that. If not, then we can simply use a call to one of the embedding models in Amazon Bedrock.
    e. The embeddings will be stored in an S3 Vectors bucket. The name of the bucket will be configurable via an ENV var, and these will be passed into the helm charts to deploy the container in K8S using the values.yaml file. The ID of the vector will be the UUID of the chunk - this way, when we implement search, we can map the results of the vector search back to the relevant chunk.
    f. We will put additional metatada on each vector:
        + user_id: the ID of the user that owns the site under which the content.md file lives. This will be extracted from the path - its always the first part of the path, i.e. /knuth/content.md. The site name is "knuth" and this will be used to lookup the user ID from Supabase, which has a user_site table that maps a user to the site.
        + path: The path to the content.md file that is being indexed
    g. For a content.md file that has previously been indexed, we need to do the following before indexing it again:
        + Remove all the existing chunks
        + Delete all the existing vectors in the S3 vector store - to do this, we'll use the "path" metadata to fetch all the vectors that already exist for the content.md file path.

### Search

Search will be implemented in this way:

1. The Chatbot in the front end is where the user will initiate a search query. The user will be able to type in prompts like "find all the sites about the YoloScribe project"
2. The backend chat API will invoke a new kind of agent, called SearchAgent, for these types of requests
3. The SearchAgent will create the embeddings of the search query and then query that vector against the S3 vector store.
4. The results that are returned from the semantic query will have a "path" meta data field on them - this will point to the relevant content.md files.
5. The search results will be stored in a file called "search.md" and this will live at the top-level of the user's site. For example, if the user's site is /knuth, then the search.md file will be placed in a path at /knuth/.user/search.md. The search.md file should contain a summary of the search results based on the chunks that correpsond to each vector returned in the search. There should be links to the relevant content.md files for that search.
6. After a search, the frontend will load the /[site]/.user/search.md file in the same way that it does agents in the .agent directory. The breadcrumb navigation display should update in the same way as well.
7. If a search.md file already exists, append to it rather than overrwrite it. Include the query that was used to generate the search results so that the user effectively has a "search history"
