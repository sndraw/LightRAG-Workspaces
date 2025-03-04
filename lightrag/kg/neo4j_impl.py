import asyncio
import inspect
import os
import re
from dataclasses import dataclass
from typing import Any, List, Dict, final
import numpy as np
import configparser


from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from ..utils import logger
from ..base import BaseGraphStorage
from ..types import KnowledgeGraph, KnowledgeGraphNode, KnowledgeGraphEdge
import pipmaster as pm

if not pm.is_installed("neo4j"):
    pm.install("neo4j")

from neo4j import (  # type: ignore
    AsyncGraphDatabase,
    exceptions as neo4jExceptions,
    AsyncDriver,
    AsyncManagedTransaction,
    GraphDatabase,
)

config = configparser.ConfigParser()
config.read("config.ini", "utf-8")

# Get maximum number of graph nodes from environment variable, default is 1000
MAX_GRAPH_NODES = int(os.getenv("MAX_GRAPH_NODES", 1000))


@final
@dataclass
class Neo4JStorage(BaseGraphStorage):
    def __init__(self, namespace, global_config, embedding_func):
        super().__init__(
            namespace=namespace,
            global_config=global_config,
            embedding_func=embedding_func,
        )
        self._driver = None
        self._driver_lock = asyncio.Lock()

        URI = os.environ.get("NEO4J_URI", config.get("neo4j", "uri", fallback=None))
        USERNAME = os.environ.get(
            "NEO4J_USERNAME", config.get("neo4j", "username", fallback=None)
        )
        PASSWORD = os.environ.get(
            "NEO4J_PASSWORD", config.get("neo4j", "password", fallback=None)
        )
        MAX_CONNECTION_POOL_SIZE = int(
            os.environ.get(
                "NEO4J_MAX_CONNECTION_POOL_SIZE",
                config.get("neo4j", "connection_pool_size", fallback=800),
            )
        )
        CONNECTION_TIMEOUT = float(
            os.environ.get(
                "NEO4J_CONNECTION_TIMEOUT",
                config.get("neo4j", "connection_timeout", fallback=60.0),
            ),
        )
        CONNECTION_ACQUISITION_TIMEOUT = float(
            os.environ.get(
                "NEO4J_CONNECTION_ACQUISITION_TIMEOUT",
                config.get("neo4j", "connection_acquisition_timeout", fallback=60.0),
            ),
        )
        DATABASE = os.environ.get(
            "NEO4J_DATABASE", re.sub(r"[^a-zA-Z0-9-]", "-", namespace)
        )

        self._driver: AsyncDriver = AsyncGraphDatabase.driver(
            URI,
            auth=(USERNAME, PASSWORD),
            max_connection_pool_size=MAX_CONNECTION_POOL_SIZE,
            connection_timeout=CONNECTION_TIMEOUT,
            connection_acquisition_timeout=CONNECTION_ACQUISITION_TIMEOUT,
        )

        # Try to connect to the database
        with GraphDatabase.driver(
            URI,
            auth=(USERNAME, PASSWORD),
            max_connection_pool_size=MAX_CONNECTION_POOL_SIZE,
            connection_timeout=CONNECTION_TIMEOUT,
            connection_acquisition_timeout=CONNECTION_ACQUISITION_TIMEOUT,
        ) as _sync_driver:
            for database in (DATABASE, None):
                self._DATABASE = database
                connected = False

                try:
                    with _sync_driver.session(database=database) as session:
                        try:
                            session.run("MATCH (n) RETURN n LIMIT 0")
                            logger.info(f"Connected to {database} at {URI}")
                            connected = True
                        except neo4jExceptions.ServiceUnavailable as e:
                            logger.error(
                                f"{database} at {URI} is not available".capitalize()
                            )
                            raise e
                except neo4jExceptions.AuthError as e:
                    logger.error(f"Authentication failed for {database} at {URI}")
                    raise e
                except neo4jExceptions.ClientError as e:
                    if e.code == "Neo.ClientError.Database.DatabaseNotFound":
                        logger.info(
                            f"{database} at {URI} not found. Try to create specified database.".capitalize()
                        )
                        try:
                            with _sync_driver.session() as session:
                                session.run(
                                    f"CREATE DATABASE `{database}` IF NOT EXISTS"
                                )
                                logger.info(f"{database} at {URI} created".capitalize())
                                connected = True
                        except (
                            neo4jExceptions.ClientError,
                            neo4jExceptions.DatabaseError,
                        ) as e:
                            if (
                                e.code
                                == "Neo.ClientError.Statement.UnsupportedAdministrationCommand"
                            ) or (
                                e.code == "Neo.DatabaseError.Statement.ExecutionFailed"
                            ):
                                if database is not None:
                                    logger.warning(
                                        "This Neo4j instance does not support creating databases. Try to use Neo4j Desktop/Enterprise version or DozerDB instead. Fallback to use the default database."
                                    )
                            if database is None:
                                logger.error(f"Failed to create {database} at {URI}")
                                raise e

                if connected:
                    break

    def __post_init__(self):
        self._node_embed_algorithms = {
            "node2vec": self._node2vec_embed,
        }

    async def close(self):
        if self._driver:
            await self._driver.close()
            self._driver = None

    async def __aexit__(self, exc_type, exc, tb):
        if self._driver:
            await self._driver.close()

    async def index_done_callback(self) -> None:
        # Noe4J handles persistence automatically
        pass

    async def _label_exists(self, label: str) -> bool:
        """Check if a label exists in the Neo4j database."""
        query = "CALL db.labels() YIELD label RETURN label"
        try:
            async with self._driver.session(database=self._DATABASE) as session:
                result = await session.run(query)
                labels = [record["label"] for record in await result.data()]
                return label in labels
        except Exception as e:
            logger.error(f"Error checking label existence: {e}")
            return False

    async def _ensure_label(self, label: str) -> str:
        """Ensure a label exists by validating it."""
        clean_label = label.strip('"')
        if not await self._label_exists(clean_label):
            logger.warning(f"Label '{clean_label}' does not exist in Neo4j")
        return clean_label

    async def has_node(self, node_id: str) -> bool:
        entity_name_label = await self._ensure_label(node_id)
        async with self._driver.session(database=self._DATABASE) as session:
            query = (
                f"MATCH (n:`{entity_name_label}`) RETURN count(n) > 0 AS node_exists"
            )
            result = await session.run(query)
            single_result = await result.single()
            logger.debug(
                f"{inspect.currentframe().f_code.co_name}:query:{query}:result:{single_result['node_exists']}"
            )
            return single_result["node_exists"]

    async def has_edge(self, source_node_id: str, target_node_id: str) -> bool:
        entity_name_label_source = source_node_id.strip('"')
        entity_name_label_target = target_node_id.strip('"')

        async with self._driver.session(database=self._DATABASE) as session:
            query = (
                f"MATCH (a:`{entity_name_label_source}`)-[r]-(b:`{entity_name_label_target}`) "
                "RETURN COUNT(r) > 0 AS edgeExists"
            )
            result = await session.run(query)
            single_result = await result.single()
            logger.debug(
                f"{inspect.currentframe().f_code.co_name}:query:{query}:result:{single_result['edgeExists']}"
            )
            return single_result["edgeExists"]

    async def get_node(self, node_id: str) -> dict[str, str] | None:
        """Get node by its label identifier.

        Args:
            node_id: The node label to look up

        Returns:
            dict: Node properties if found
            None: If node not found
        """
        async with self._driver.session(database=self._DATABASE) as session:
            entity_name_label = await self._ensure_label(node_id)
            query = f"MATCH (n:`{entity_name_label}`) RETURN n"
            result = await session.run(query)
            record = await result.single()
            if record:
                node = record["n"]
                node_dict = dict(node)
                logger.debug(
                    f"{inspect.currentframe().f_code.co_name}: query: {query}, result: {node_dict}"
                )
                return node_dict
            return None

    async def node_degree(self, node_id: str) -> int:
        entity_name_label = node_id.strip('"')

        async with self._driver.session(database=self._DATABASE) as session:
            query = f"""
                MATCH (n:`{entity_name_label}`)
                RETURN COUNT{{ (n)--() }} AS totalEdgeCount
            """
            result = await session.run(query)
            record = await result.single()
            if record:
                edge_count = record["totalEdgeCount"]
                logger.debug(
                    f"{inspect.currentframe().f_code.co_name}:query:{query}:result:{edge_count}"
                )
                return edge_count
            else:
                return None

    async def edge_degree(self, src_id: str, tgt_id: str) -> int:
        entity_name_label_source = src_id.strip('"')
        entity_name_label_target = tgt_id.strip('"')
        src_degree = await self.node_degree(entity_name_label_source)
        trg_degree = await self.node_degree(entity_name_label_target)

        # Convert None to 0 for addition
        src_degree = 0 if src_degree is None else src_degree
        trg_degree = 0 if trg_degree is None else trg_degree

        degrees = int(src_degree) + int(trg_degree)
        logger.debug(
            f"{inspect.currentframe().f_code.co_name}:query:src_Degree+trg_degree:result:{degrees}"
        )
        return degrees

    async def get_edge(
        self, source_node_id: str, target_node_id: str
    ) -> dict[str, str] | None:
        try:
            entity_name_label_source = source_node_id.strip('"')
            entity_name_label_target = target_node_id.strip('"')

            async with self._driver.session(database=self._DATABASE) as session:
                query = f"""
                MATCH (start:`{entity_name_label_source}`)-[r]->(end:`{entity_name_label_target}`)
                RETURN properties(r) as edge_properties
                LIMIT 1
                """

                result = await session.run(query)
                record = await result.single()
                if record:
                    try:
                        result = dict(record["edge_properties"])
                        logger.info(f"Result: {result}")
                        # Ensure required keys exist with defaults
                        required_keys = {
                            "weight": 0.0,
                            "source_id": None,
                            "description": None,
                            "keywords": None,
                        }
                        for key, default_value in required_keys.items():
                            if key not in result:
                                result[key] = default_value
                                logger.warning(
                                    f"Edge between {entity_name_label_source} and {entity_name_label_target} "
                                    f"missing {key}, using default: {default_value}"
                                )

                        logger.debug(
                            f"{inspect.currentframe().f_code.co_name}:query:{query}:result:{result}"
                        )
                        return result
                    except (KeyError, TypeError, ValueError) as e:
                        logger.error(
                            f"Error processing edge properties between {entity_name_label_source} "
                            f"and {entity_name_label_target}: {str(e)}"
                        )
                        # Return default edge properties on error
                        return {
                            "weight": 0.0,
                            "description": None,
                            "keywords": None,
                            "source_id": None,
                        }

                logger.debug(
                    f"{inspect.currentframe().f_code.co_name}: No edge found between {entity_name_label_source} and {entity_name_label_target}"
                )
                # Return default edge properties when no edge found
                return {
                    "weight": 0.0,
                    "description": None,
                    "keywords": None,
                    "source_id": None,
                }

        except Exception as e:
            logger.error(
                f"Error in get_edge between {source_node_id} and {target_node_id}: {str(e)}"
            )
            # Return default edge properties on error
            return {
                "weight": 0.0,
                "description": None,
                "keywords": None,
                "source_id": None,
            }

    async def get_node_edges(self, source_node_id: str) -> list[tuple[str, str]] | None:
        node_label = source_node_id.strip('"')

        """
        Retrieves all edges (relationships) for a particular node identified by its label.
        :return: List of dictionaries containing edge information
        """
        query = f"""MATCH (n:`{node_label}`)
                OPTIONAL MATCH (n)-[r]-(connected)
                RETURN n, r, connected"""
        async with self._driver.session(database=self._DATABASE) as session:
            results = await session.run(query)
            edges = []
            async for record in results:
                source_node = record["n"]
                connected_node = record["connected"]

                source_label = (
                    list(source_node.labels)[0] if source_node.labels else None
                )
                target_label = (
                    list(connected_node.labels)[0]
                    if connected_node and connected_node.labels
                    else None
                )

                if source_label and target_label:
                    edges.append((source_label, target_label))

            return edges

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(
            (
                neo4jExceptions.ServiceUnavailable,
                neo4jExceptions.TransientError,
                neo4jExceptions.WriteServiceUnavailable,
                neo4jExceptions.ClientError,
            )
        ),
    )
    async def upsert_node(self, node_id: str, node_data: dict[str, str]) -> None:
        """
        Upsert a node in the Neo4j database.

        Args:
            node_id: The unique identifier for the node (used as label)
            node_data: Dictionary of node properties
        """
        label = await self._ensure_label(node_id)
        properties = node_data

        async def _do_upsert(tx: AsyncManagedTransaction):
            query = f"""
            MERGE (n:`{label}`)
            SET n += $properties
            """
            await tx.run(query, properties=properties)
            logger.debug(
                f"Upserted node with label '{label}' and properties: {properties}"
            )

        try:
            async with self._driver.session(database=self._DATABASE) as session:
                await session.execute_write(_do_upsert)
        except Exception as e:
            logger.error(f"Error during upsert: {str(e)}")
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(
            (
                neo4jExceptions.ServiceUnavailable,
                neo4jExceptions.TransientError,
                neo4jExceptions.WriteServiceUnavailable,
                neo4jExceptions.ClientError,
            )
        ),
    )
    async def upsert_edge(
        self, source_node_id: str, target_node_id: str, edge_data: dict[str, str]
    ) -> None:
        """
        Upsert an edge and its properties between two nodes identified by their labels.

        Args:
            source_node_id (str): Label of the source node (used as identifier)
            target_node_id (str): Label of the target node (used as identifier)
            edge_data (dict): Dictionary of properties to set on the edge
        """
        source_label = await self._ensure_label(source_node_id)
        target_label = await self._ensure_label(target_node_id)
        edge_properties = edge_data

        async def _do_upsert_edge(tx: AsyncManagedTransaction):
            query = f"""
            MATCH (source:`{source_label}`)
            WITH source
            MATCH (target:`{target_label}`)
            MERGE (source)-[r:DIRECTED]->(target)
            SET r += $properties
            RETURN r
            """
            result = await tx.run(query, properties=edge_properties)
            record = await result.single()
            logger.debug(
                f"Upserted edge from '{source_label}' to '{target_label}' with properties: {edge_properties}, result: {record['r'] if record else None}"
            )

        try:
            async with self._driver.session(database=self._DATABASE) as session:
                await session.execute_write(_do_upsert_edge)
        except Exception as e:
            logger.error(f"Error during edge upsert: {str(e)}")
            raise

    async def _node2vec_embed(self):
        print("Implemented but never called.")

    async def get_knowledge_graph(
        self, node_label: str, max_depth: int = 5
    ) -> KnowledgeGraph:
        """
        Retrieve a connected subgraph of nodes where the label includes the specified `node_label`.
        Maximum number of nodes is constrained by the environment variable `MAX_GRAPH_NODES` (default: 1000).
        When reducing the number of nodes, the prioritization criteria are as follows:
            1. Label matching nodes take precedence (nodes containing the specified label string)
            2. Followed by nodes directly connected to the matching nodes
            3. Finally, the degree of the nodes

        Args:
            node_label (str): String to match in node labels (will match any node containing this string in its label)
            max_depth (int, optional): Maximum depth of the graph. Defaults to 5.
        Returns:
            KnowledgeGraph: Complete connected subgraph for specified node
        """
        label = node_label.strip('"')
        # Escape single quotes to prevent injection attacks
        escaped_label = label.replace("'", "\\'")
        result = KnowledgeGraph()
        seen_nodes = set()
        seen_edges = set()

        async with self._driver.session(database=self._DATABASE) as session:
            try:
                if label == "*":
                    main_query = """
                    MATCH (n)
                    OPTIONAL MATCH (n)-[r]-()
                    WITH n, count(r) AS degree
                    ORDER BY degree DESC
                    LIMIT $max_nodes
                    WITH collect(n) AS nodes
                    MATCH (a)-[r]->(b)
                    WHERE a IN nodes AND b IN nodes
                    RETURN nodes, collect(DISTINCT r) AS relationships
                    """
                    result_set = await session.run(
                        main_query, {"max_nodes": MAX_GRAPH_NODES}
                    )

                else:
                    validate_query = f"""
                    MATCH (n)
                    WHERE any(label IN labels(n) WHERE label CONTAINS '{escaped_label}')
                    RETURN n LIMIT 1
                    """
                    validate_result = await session.run(validate_query)
                    if not await validate_result.single():
                        logger.warning(
                            f"No nodes containing '{label}' in their labels found!"
                        )
                        return result

                    # Main query uses partial matching
                    main_query = f"""
                    MATCH (start)
                    WHERE any(label IN labels(start) WHERE label CONTAINS '{escaped_label}')
                    WITH start
                    CALL apoc.path.subgraphAll(start, {{
                        relationshipFilter: '>',
                        minLevel: 0,
                        maxLevel: {max_depth},
                        bfs: true
                    }})
                    YIELD nodes, relationships
                    WITH start, nodes, relationships
                    UNWIND nodes AS node
                    OPTIONAL MATCH (node)-[r]-()
                    WITH node, count(r) AS degree, start, nodes, relationships,
                            CASE
                            WHEN id(node) = id(start) THEN 2
                            WHEN EXISTS((start)-->(node)) OR EXISTS((node)-->(start)) THEN 1
                            ELSE 0
                            END AS priority
                    ORDER BY priority DESC, degree DESC
                    LIMIT $max_nodes
                    WITH collect(node) AS filtered_nodes, nodes, relationships
                    RETURN filtered_nodes AS nodes,
                            [rel IN relationships WHERE startNode(rel) IN filtered_nodes AND endNode(rel) IN filtered_nodes] AS relationships
                    """
                    result_set = await session.run(
                        main_query, {"max_nodes": MAX_GRAPH_NODES}
                    )

                record = await result_set.single()

                if record:
                    # Handle nodes (compatible with multi-label cases)
                    for node in record["nodes"]:
                        # Use node ID + label combination as unique identifier
                        node_id = node.id
                        if node_id not in seen_nodes:
                            result.nodes.append(
                                KnowledgeGraphNode(
                                    id=f"{node_id}",
                                    labels=list(node.labels),
                                    properties=dict(node),
                                )
                            )
                            seen_nodes.add(node_id)

                    # Handle relationships (including direction information)
                    for rel in record["relationships"]:
                        edge_id = rel.id
                        if edge_id not in seen_edges:
                            start = rel.start_node
                            end = rel.end_node
                            result.edges.append(
                                KnowledgeGraphEdge(
                                    id=f"{edge_id}",
                                    type=rel.type,
                                    source=f"{start.id}",
                                    target=f"{end.id}",
                                    properties=dict(rel),
                                )
                            )
                            seen_edges.add(edge_id)

                    logger.info(
                        f"Subgraph query successful | Node count: {len(result.nodes)} | Edge count: {len(result.edges)}"
                    )

            except neo4jExceptions.ClientError as e:
                logger.error(f"APOC query failed: {str(e)}")
                return await self._robust_fallback(label, max_depth)

        return result

    async def _robust_fallback(
        self, label: str, max_depth: int
    ) -> Dict[str, List[Dict]]:
        """Enhanced fallback query solution"""
        result = {"nodes": [], "edges": []}
        visited_nodes = set()
        visited_edges = set()

        async def traverse(current_label: str, current_depth: int):
            if current_depth > max_depth:
                return

            # Get current node details
            node = await self.get_node(current_label)
            if not node:
                return

            node_id = f"{current_label}"
            if node_id in visited_nodes:
                return
            visited_nodes.add(node_id)

            # Add node data (with complete labels)
            node_data = {k: v for k, v in node.items()}
            node_data["labels"] = [
                current_label
            ]  # Assume get_node method returns label information
            result["nodes"].append(node_data)

            # Get all outgoing and incoming edges
            query = f"""
            MATCH (a)-[r]-(b)
            WHERE a:`{current_label}` OR b:`{current_label}`
            RETURN a, r, b,
                   CASE WHEN startNode(r) = a THEN 'OUTGOING' ELSE 'INCOMING' END AS direction
            """
            async with self._driver.session(database=self._DATABASE) as session:
                results = await session.run(query)
                async for record in results:
                    # Handle edges
                    rel = record["r"]
                    edge_id = f"{rel.id}_{rel.type}"
                    if edge_id not in visited_edges:
                        edge_data = dict(rel)
                        edge_data.update(
                            {
                                "source": list(record["a"].labels)[0],
                                "target": list(record["b"].labels)[0],
                                "type": rel.type,
                                "direction": record["direction"],
                            }
                        )
                        result["edges"].append(edge_data)
                        visited_edges.add(edge_id)

                        # Recursively traverse adjacent nodes
                        next_label = (
                            list(record["b"].labels)[0]
                            if record["direction"] == "OUTGOING"
                            else list(record["a"].labels)[0]
                        )
                        await traverse(next_label, current_depth + 1)

        await traverse(label, 0)
        return result

    async def get_all_labels(self) -> list[str]:
        """
        Get all existing node labels in the database
        Returns:
            ["Person", "Company", ...]  # Alphabetically sorted label list
        """
        async with self._driver.session(database=self._DATABASE) as session:
            # Method 1: Direct metadata query (Available for Neo4j 4.3+)
            # query = "CALL db.labels() YIELD label RETURN label"

            # Method 2: Query compatible with older versions
            query = """
                MATCH (n)
                WITH DISTINCT labels(n) AS node_labels
                UNWIND node_labels AS label
                RETURN DISTINCT label
                ORDER BY label
            """

            result = await session.run(query)
            labels = []
            async for record in result:
                labels.append(record["label"])
            return labels

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(
            (
                neo4jExceptions.ServiceUnavailable,
                neo4jExceptions.TransientError,
                neo4jExceptions.WriteServiceUnavailable,
                neo4jExceptions.ClientError,
            )
        ),
    )
    async def delete_node(self, node_id: str) -> None:
        """Delete a node with the specified label

        Args:
            node_id: The label of the node to delete
        """
        label = await self._ensure_label(node_id)

        async def _do_delete(tx: AsyncManagedTransaction):
            query = f"""
            MATCH (n:`{label}`)
            DETACH DELETE n
            """
            await tx.run(query)
            logger.debug(f"Deleted node with label '{label}'")

        try:
            async with self._driver.session(database=self._DATABASE) as session:
                await session.execute_write(_do_delete)
        except Exception as e:
            logger.error(f"Error during node deletion: {str(e)}")
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(
            (
                neo4jExceptions.ServiceUnavailable,
                neo4jExceptions.TransientError,
                neo4jExceptions.WriteServiceUnavailable,
                neo4jExceptions.ClientError,
            )
        ),
    )
    async def remove_nodes(self, nodes: list[str]):
        """Delete multiple nodes

        Args:
            nodes: List of node labels to be deleted
        """
        for node in nodes:
            await self.delete_node(node)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(
            (
                neo4jExceptions.ServiceUnavailable,
                neo4jExceptions.TransientError,
                neo4jExceptions.WriteServiceUnavailable,
                neo4jExceptions.ClientError,
            )
        ),
    )
    async def remove_edges(self, edges: list[tuple[str, str]]):
        """Delete multiple edges

        Args:
            edges: List of edges to be deleted, each edge is a (source, target) tuple
        """
        for source, target in edges:
            source_label = await self._ensure_label(source)
            target_label = await self._ensure_label(target)

            async def _do_delete_edge(tx: AsyncManagedTransaction):
                query = f"""
                MATCH (source:`{source_label}`)-[r]->(target:`{target_label}`)
                DELETE r
                """
                await tx.run(query)
                logger.debug(f"Deleted edge from '{source_label}' to '{target_label}'")

            try:
                async with self._driver.session(database=self._DATABASE) as session:
                    await session.execute_write(_do_delete_edge)
            except Exception as e:
                logger.error(f"Error during edge deletion: {str(e)}")
                raise

    async def embed_nodes(
        self, algorithm: str
    ) -> tuple[np.ndarray[Any, Any], list[str]]:
        raise NotImplementedError


    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(
            (
                neo4jExceptions.ServiceUnavailable,
                neo4jExceptions.TransientError,
                neo4jExceptions.WriteServiceUnavailable,
            )
        ),
    )
    async def delete_all(self):
        # 删除全部实体和关系
        query = f"""
            MATCH (n)-[r]->(m)
            DELETE n, r, m
            RETURN n as source_node, r as relationship, m as target_node
            UNION ALL
            MATCH (n)
            WHERE NOT (n)--()
            DELETE n
            RETURN n as source_node, null as relationship, null as target_node
            """
        try:
            async with self._driver.session(database=self._DATABASE) as session:
                await session.run(query)
        except Exception as e:
            logger.error(f"Error during delete all: {str(e)}")
            raise


    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(
            (
                neo4jExceptions.ServiceUnavailable,
                neo4jExceptions.TransientError,
                neo4jExceptions.WriteServiceUnavailable,
            )
        ),
    )
    async def query_all(self) -> List[Dict[str, Any]]:
        """
        Retrieves all nodes and their relationships in the Neo4j database.

        Returns:
            list: List of dictionaries containing node and relationship information
        """
        query = f"""
            MATCH (n)-[r]->(m)
            RETURN n AS source_node, r AS relationship, m AS target_node
            UNION
            MATCH (n)
            WHERE NOT (n)--()
            RETURN n AS source_node, null AS relationship, null AS target_node
            """
        try:
            async with self._driver.session(database=self._DATABASE) as session:
                result = await session.run(query)
                entities = []
                async for record in result:
                    s_node = record["source_node"]
                    r_ship = record["relationship"]
                    t_node = record["target_node"]
                    # 获取源节点信息
                    source_node = {
                        "id": (
                            s_node.element_id
                            if s_node and hasattr(s_node, "element_id")
                            else None
                        ),
                        "labels": (
                            list(s_node.labels)
                            if s_node and hasattr(s_node, "labels")
                            else []
                        ),
                        "properties": dict(s_node.items()) if s_node else None,
                    }
                    # 获取关系信息
                    relationship = (
                        {
                            "id": (
                                r_ship.element_id
                                if r_ship and hasattr(r_ship, "element_id")
                                else None
                            ),
                            "type": (
                                r_ship.type
                                if r_ship and hasattr(r_ship, "type")
                                else None
                            ),
                            "properties": dict(r_ship.items()) if r_ship else None,
                        }
                        if r_ship
                        else None
                    )

                    # 获取目标节点信息
                    target_node = (
                        {
                            "id": (
                                t_node.element_id
                                if t_node and hasattr(t_node, "element_id")
                                else None
                            ),
                            "labels": (
                                list(t_node.labels)
                                if t_node and hasattr(t_node, "labels")
                                else []
                            ),
                            "properties": dict(t_node.items()) if t_node else None,
                        }
                        if t_node
                        else None
                    )

                    entities.append(
                        {
                            "source_node": source_node,
                            "relationship": relationship,
                            "target_node": target_node,
                        }
                    )

                return entities
        except Exception as e:
            logger.error(f"Error occurred while querying all nodes: {e}")

        async with self._driver.session(database=self._DATABASE) as session:
            entity_name_label = node_label.strip('"')
            query = f"""
                MATCH (n:`{entity_name_label}`) RETURN n
                """
            result = await session.run(query)
            record = await result.single()
            if record:
                node = record["n"]
                node_dict = {
                    "id": (
                        node.element_id
                        if node and hasattr(node, "element_id")
                        else None
                    ),
                    "labels": (
                        list(node.labels) if node and hasattr(node, "labels") else []
                    ),
                    "properties": dict(node.items()) if node else None,
                }
                logger.debug(
                    f"{inspect.currentframe().f_code.co_name}: query: {query}, result: {node_dict}"
                )
                return node_dict
            return None