import os
import re
import concurrent.futures
import pickle
import threading
import time
from pathlib import Path


def default_file_filter(file_paths):
    # UE项目中常见的源代码文件扩展名
    file_extensions = {'.cpp', '.h', '.hpp', '.c', '.cc', '.cxx'}

    # 需要排除的目录
    exclude_dirs = {
        'Binaries', 'Build', 'DerivedDataCache', 'Intermediate',
        'Saved', '.git', '.vs', '.vscode', 'node_modules'
    }
    def should_search(file_path):
        file_path = Path(file_path)

        if file_path.suffix.lower() not in file_extensions:
            return False

        # 检查是否在排除的目录中
        for part in file_path.parts:
            if part in exclude_dirs:
                return False

        return True

    return [path for path in file_paths if should_search(path)]



class RegexSearchTool:

    class ContextSearchOptions:
        def __init__(self,
                    context_chars_before, 
                    context_chars_after,
                    context_regex_filters = []):
            self.context_chars_before = context_chars_before
            self.context_chars_after = context_chars_after
            self.context_regex_filters = context_regex_filters # [(regex, group_index), ...]


    def __init__(self):
        self.lock = threading.Lock() # For thread-safe cache updates

    def _get_file_metadata_hash(self, filepath):
        """
        Generates a hash for a file based on its modification time and size.
        This is a lightweight hash, NOT a content hash like MD5.
        """
        try:
            stat_info = os.stat(filepath)
            # Combine modification time (mtime) and file size
            # Using str() and then hashing to get a consistent integer hash
            return hash(f"{stat_info.st_mtime}-{stat_info.st_size}")
        except FileNotFoundError:
            return None # File might have been deleted
        except Exception as e:
            print(f"Error getting metadata for {filepath}: {e}")
            return None

    # def _calculate_folder_metadata_hash_and_file_info(self, folder_path):
    #     """
    #     Calculates a "virtual" hash for a folder based on its contents' metadata
    #     and collects file information.
    #     Returns (folder_hash, file_count, files_to_process).
    #     """
    #     file_count = 0
    #     files_to_process = [] # List of (filepath, file_metadata_hash) tuples

    #     try:
    #         for root, dirs, files in os.walk(folder_path):
    #             # Process files
    #             for file_name in sorted(files): # Sort for consistent hash
    #                 filepath = os.path.join(root, file_name)
    #                 # file_hash = self._get_file_metadata_hash(filepath)
    #                 # if file_hash is not None:
    #                 file_count += 1
    #                 files_to_process.append(filepath) # Store just the path for searching later
    #     except Exception as e:
    #         print(f"Error walking directory {folder_path}: {e}")
    #         return None, 0, []

    #     # Generate a combined hash for the folder's structure and its immediate children's metadata
    #     # Using a simple hash of the joined string
    #     # combined_info_string = "|".join(folder_items_info)
    #     # folder_hash = hash(combined_info_string)

    #     return folder_hash, file_count, files_to_process
    
    def _get_files(self, root_dir):
        """
        Returns a list of all files in the directory and its subdirectories.
        """
        all_files = []
        for dirpath, _, filenames in os.walk(root_dir):
            for filename in filenames:
                all_files.append(os.path.join(dirpath, filename))
        return all_files






    def _search_file_task(self, file_paths, regex_pattern,index_group=0,context_search_options:ContextSearchOptions=None):
        """Task for a single thread: searches multiple files."""
        thread_matches = {}
        compiled_regex = re.compile(regex_pattern)
        try:
            if(context_search_options is None):
                for filepath in file_paths:
                    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                        matches = re.finditer(compiled_regex, content)
                        for match in matches:
                            # Store the match with its line number and context
                            line_number = content.count('\n', 0, match.start()) + 1
                            thread_matches.setdefault(match.group(index_group), []).append({
                                'file': filepath,
                                'line': line_number,
                            })
            else:
                def get_context(content, match, context_chars_before, context_chars_after):
                    """Extracts context lines around a match."""
                    start = max(0, match.start() - context_chars_before)
                    end = min(len(content), match.end() + context_chars_after)
                    return content[start:end]
                for filepath in file_paths:
                    compiled_context_regex_filters = [(re.compile(regex), group_index) for regex, group_index in context_search_options.context_regex_filters]
                    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                        matches = re.finditer(compiled_regex, content)
                        for match in matches:
                            # Store the match with its line number and context
                            line_number = content.count('\n', 0, match.start()) + 1
                            context = get_context(content, match,
                                                  context_search_options.context_chars_before,
                                                  context_search_options.context_chars_after)
                            context_search_results = []
                            for cp_regex, group_index in compiled_context_regex_filters:
                                context_matches = re.finditer(cp_regex, context)
                                context_search_results.append([m.group(group_index) for m in context_matches if m])

                            thread_matches.setdefault(match.group(index_group), []).append({
                                'file': filepath,
                                'line': line_number,
                                'context_info': context_search_results
                            })

        except Exception as e:
            print(f"Error reading file {filepath}: {e}")
        return thread_matches

    def search(self, root_dir, regex_pattern,index_group=0,context_search_options:ContextSearchOptions=None,file_filter=default_file_filter):
        """
        Performs a regex search in the given directory, leveraging caching
        and multithreading based on logical CPU cores, using metadata for hash.
        """
        all_matches = {}
        # folder_cache_key = f"folder_info_{root_dir}" # Unique key for the root folder's info

        # --- Indexing Phase (using metadata hash) ---
        print(f"[{time.strftime('%H:%M:%S')}] Starting indexing phase (using metadata hash)...")
        # current_folder_hash, current_file_count, all_project_files = \
        #     self._calculate_folder_metadata_hash_and_file_info(root_dir)
        
        all_project_files = self._get_files(root_dir)
        all_project_files = file_filter(all_project_files)


        # --- Threaded Searching Phase ---
        num_threads = os.cpu_count()*2 or 1 # Use logical core count, default to 1
        print(f"[{time.strftime('%H:%M:%S')}] Using {num_threads} threads for search.")

        # Distribute files evenly among threads
        file_batches = [[] for _ in range(num_threads)]
        for i, filepath in enumerate(all_project_files):
            file_batches[i % num_threads].append(filepath)

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            future_to_batch = {executor.submit(self._search_file_task, batch, regex_pattern, index_group, context_search_options): batch for batch in file_batches if batch}

            for future in concurrent.futures.as_completed(future_to_batch):
                try:
                    thread_result = future.result()
                    with self.lock: # Protect shared 'all_matches' and cache update
                        all_matches.update(thread_result)
                except Exception as exc:
                    print(f"[{time.strftime('%H:%M:%S')}] A thread generated an exception: {exc}")

        # Update cache with new results
        # with self.lock:
        #     self.cache[folder_cache_key]["results"][regex_pattern] = all_matches
        #     self._save_cache()

        print(f"[{time.strftime('%H:%M:%S')}] Search complete for '{root_dir}'.")
        return all_matches

# --- Usage Example ---
if __name__ == "__main__":

    tool = RegexSearchTool()
    search_dir = r"C:\Users\lyq\UESource\UnrealEngine-5.4.4-release"
    regex_pattern = r"CSV_SCOPED_TIMING_STAT_EXCLUSIVE\((.*?)\)"
    context_search_options = RegexSearchTool.ContextSearchOptions(
        context_chars_before=500, 
        context_chars_after=500,
        context_regex_filters=[(r"SCOPED_NAMED_EVENT\(([^,]+?),.*?\)", 1)]
    )

    start_time = time.time()
    results1 = tool.search(search_dir, regex_pattern, 1, context_search_options)
    end_time = time.time()
    print(f"First search took: {end_time - start_time:.2f} seconds")
    # For demonstration, print only first 3 results
    for i, (index, info) in enumerate(results1.items()):
        print(f"Index: {index}, Matches: {info}")