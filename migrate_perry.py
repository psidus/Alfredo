"""
Migrate Perry_8th collection from local Qdrant file storage to Qdrant server.
Run this OUTSIDE Docker, directly on Windows, to avoid Docker volume I/O bottleneck.
"""
import sys
import time

try:
    from qdrant_client import QdrantClient
    from qdrant_client.http.models import Distance, VectorParams, PointStruct
except ImportError:
    print("Installing qdrant-client...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "qdrant-client"])
    from qdrant_client import QdrantClient
    from qdrant_client.http.models import Distance, VectorParams, PointStruct

LOCAL_PATH = "storage/vector_dbs/qdrant_local"
SERVER_URL = "http://localhost:6333"  # Qdrant server exposed on host port
COLLECTION_NAME = "Perry_8th"
BATCH_SIZE = 1

print(f"Opening local Qdrant database at: {LOCAL_PATH}")
t0 = time.time()
try:
    local_client = QdrantClient(path=LOCAL_PATH)
    print(f"Local DB opened in {time.time()-t0:.1f}s")
except Exception as e:
    print(f"ERROR opening local DB: {e}")
    sys.exit(1)

# Get collection info
try:
    info = local_client.get_collection(COLLECTION_NAME)
    total_points = info.points_count
    vector_size = info.config.params.vectors.size
    print(f"Collection '{COLLECTION_NAME}': {total_points} points, vector size: {vector_size}")
except Exception as e:
    print(f"ERROR getting collection info: {e}")
    sys.exit(1)

# Connect to remote server
print(f"\nConnecting to Qdrant server at: {SERVER_URL}")
remote_client = QdrantClient(url=SERVER_URL, timeout=60)
server_info = remote_client.get_collections()
print(f"Server has {len(server_info.collections)} collections")

# Create collection on server if it doesn't exist
if not remote_client.collection_exists(COLLECTION_NAME):
    print(f"Creating collection '{COLLECTION_NAME}' on server...")
    remote_client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
    )
    print("Collection created!")
else:
    remote_info = remote_client.get_collection(COLLECTION_NAME)
    print(f"Collection already exists on server with {remote_info.points_count} points")
    if remote_info.points_count == total_points:
        print("Collection already fully migrated. Skipping.")
        sys.exit(0)

# Migrate data in batches
print(f"\nMigrating {total_points} points in batches of {BATCH_SIZE}...")
migrated = 0
offset = None

while True:
    # Read batch from local
    results, next_offset = local_client.scroll(
        collection_name=COLLECTION_NAME,
        limit=BATCH_SIZE,
        offset=offset,
        with_payload=True,
        with_vectors=True,
    )
    
    if not results:
        break
    
    # Convert to PointStruct for upsert
    points = [
        PointStruct(
            id=point.id,
            vector=point.vector,
            payload=point.payload,
        )
        for point in results
    ]
    
    # Write batch to server with retry
    retries = 3
    for attempt in range(retries):
        try:
            remote_client.upsert(
                collection_name=COLLECTION_NAME,
                points=points,
            )
            break
        except Exception as e:
            if attempt == retries - 1:
                print(f"Failed to upsert after {retries} attempts: {e}")
                raise
            print(f"Upsert failed: {e}. Retrying in 2 seconds...")
            time.sleep(2)
            # recreate client in case connection is dead
            remote_client = QdrantClient(url=SERVER_URL, timeout=60)
    
    migrated += len(points)
    elapsed = time.time() - t0
    rate = migrated / elapsed if elapsed > 0 else 0
    print(f"  Migrated {migrated}/{total_points} points ({rate:.0f} pts/s)")
    
    offset = next_offset
    if offset is None:
        break

# Verify
remote_info = remote_client.get_collection(COLLECTION_NAME)
print(f"\n Migration complete!")
print(f"   Local:  {total_points} points")
print(f"   Server: {remote_info.points_count} points")
print(f"   Time:   {time.time()-t0:.1f}s")

# Close local client to release lock
del local_client
